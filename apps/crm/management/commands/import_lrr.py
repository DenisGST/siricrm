"""
Импорт территориальных подразделений лицензионно-разрешительной работы
(ОЛРР / ЦЛРР / ОВЛРР) Росгвардии по каждому субъекту РФ.

Источник: региональные сайты <subdomain>.rosguard.gov.ru
Субдомен — двузначный код субъекта РФ (01, 16, 77, 78, 92 и т. д.).

Алгоритм:
1. Перебираем 85 субъектов РФ по Region.number.
2. Для каждого: главная → ищем ссылку, указывающую на страницу
   «Сведения о территориальных подразделениях ЛРР» (или аналог).
3. Эта страница может быть:
   a) Хабом со ссылками на отдельные страницы ОЛРР (Москва) —
      тогда обходим ссылки.
   b) Inline-страницей с `<b>ОЛРР/ЦЛРР ...</b>` и блоками адрес/телефон —
      тогда парсим inline.
4. Для каждого подразделения — DaData по названию (факультативно).
5. Сохраняем LegalEntity(kind=ЛРР, region=…).

HTML у регионов разнородный, поэтому парсинг «best-effort».
Часть подразделений окажется без адреса/телефона — это нормально.
"""
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.crm.models import LegalEntity, LegalEntityKind, Region
from apps.crm.management.commands.assign_legal_entity_regions import (
    find_region_number,
)


UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "text/html"}

# DaData suggest для обогащения (структурные единицы без отдельного ИНН,
# но родительское ГУ Росгвардии найдётся).
SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"

# Маркёры названия подразделения — ищем в начале <b>/<strong>.
UNIT_RE = re.compile(
    r"""
    (
      ОЛРР[^<\n]{0,180}
      | ЦЛРР[^<\n]{0,180}
      | ОВЛРР[^<\n]{0,180}
      | Отдел(?:ение)?\s+лицензионн[^<\n]{0,200}
      | Центр\s+лицензионно[^<\n]{0,200}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Телефон: 8(...) / 8-... / +7 ...
PHONE_RE = re.compile(r"(?:8|\+7)[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2,4}[\s\-]*\d{0,2}")

# Адрес: строка с 'ул.' или 'пр.' или 'пр-кт' или 'г.' + последующий текст.
# Часто предваряется индексом (6 цифр).
ADDR_RE = re.compile(
    r"(?:\d{6},?\s*)?"
    r"(?:г\.?\s*[А-ЯЁа-яё\-]+|[А-ЯЁа-яё\-]+(?:ская|ский|ое)\s+(?:обл|область|край|респ))"
    r"[^<\n]{5,250}"
    r"(?:ул\.?|ул\s|пр-?кт|переулок|пер\.|проспект|ш\.|шоссе|мкр|д\.|дом|стр\.|корп\.|наб\.?)"
    r"[^<\n]{0,120}"
)

# Более простая эвристика: ищем фрагмент с "ул." или "пр." или "д." и индексом.
ADDR_RE_LOOSE = re.compile(
    r"(?:\d{6}[,\s]+)?"
    r"[А-ЯЁа-яё][^<\n]{10,250}"
    r"(?:ул\.?|ул\s|пр-?кт|проспект|пер\.|переулок|шоссе|ш\.|наб\.?|д\.\s*\d|дом\s*\d)"
    r"[^<\n]{0,120}"
)


def fetch(url: str, timeout: int = 20) -> str | None:
    """GET с UA, возвращает HTML или None при ошибке."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


def discover_lrr_pages(subdomain: str) -> list[str]:
    """Возвращает список URL-кандидатов для страницы с ЛРР-подразделениями,
    от самого специфичного к самому общему."""
    home = fetch(f"https://{subdomain}.rosguard.gov.ru/")
    if not home:
        return []

    hrefs = set(re.findall(
        r'href="(/page/[Ii]ndex/[^"]*(?:licenzionno[- ]?razresh|licenzionnorazresh)[^"]*)"',
        home,
    ))
    # Фильтруем шум (новости, отзывы, реестры).
    hrefs = {h for h in hrefs if not re.search(
        r"(reestr|news|ocenka|perechen|provedenii|proverk|priglasha|sotrudnik|priyom|priem-grazhdan|priyom-grazhdan|otkryli|vakansii)",
        h, re.I,
    )}
    if not hrefs:
        return []

    candidates = sorted(hrefs, key=lambda h: (
        0 if "territor" in h and "svedeniya" in h else
        1 if "territor" in h else
        2 if "svedeniya" in h else
        3 if "centr-licenz" in h else
        4,
        len(h),
    ))
    return [f"https://{subdomain}.rosguard.gov.ru{h}" for h in candidates]


def discover_lrr_page(subdomain: str) -> str | None:
    """Обёртка для совместимости — возвращает первый URL-кандидат."""
    lst = discover_lrr_pages(subdomain)
    return lst[0] if lst else None


def parse_hq_from_home(subdomain: str, region_name: str) -> dict | None:
    """Fallback: из главной страницы вытаскиваем адрес/телефон головного
    Управления/ГУ Росгвардии по региону. Используем, когда специализированные
    страницы ЛРР пустые."""
    home = fetch(f"https://{subdomain}.rosguard.gov.ru/")
    if not home:
        return None
    # Адрес в футере: <li class="... footer-sprite ... address ...">
    addr = ""
    m = re.search(
        r'<li\s+class="[^"]*footer-sprite[^"]*address[^"]*"[^>]*>(.*?)</li>',
        home, re.DOTALL | re.IGNORECASE,
    )
    if m:
        addr = strip_tags(m.group(1))
        addr = re.sub(r"\s+", " ", addr).strip(" ,")

    phone = ""
    m = re.search(
        r'<li\s+class="[^"]*footer-sprite[^"]*phone[^"]*"[^>]*>(.*?)</li>',
        home, re.DOTALL | re.IGNORECASE,
    )
    if m:
        text = strip_tags(m.group(1))
        pm = PHONE_RE.search(text)
        if pm:
            phone = pm.group(0).strip()

    if not addr and not phone:
        return None

    return {
        "name": f"ЦЛРР Управления Росгвардии по {region_name}",
        "address": addr[:300],
        "phone": phone[:20],
        "source_url": f"https://{subdomain}.rosguard.gov.ru/",
    }


def strip_tags(html: str) -> str:
    """Грубое удаление HTML-тегов, сохранение переносов на <br> и </p>."""
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|tr|td|h[1-6])\s*>", "\n", html)
    html = re.sub(r"<[^>]+>", "", html)
    html = (html.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&quot;", '"')
                .replace("&#x9;", " ")
                .replace("&mdash;", "—")
                .replace("&ndash;", "–")
                .replace("&laquo;", "«")
                .replace("&raquo;", "»")
                .replace("&#x2013;", "–"))
    html = re.sub(r"\t+", " ", html)
    html = re.sub(r"[  ]+", " ", html)
    html = re.sub(r"\n{2,}", "\n", html)
    return html.strip()


def find_subpage_links(html: str, base_url: str) -> list[str]:
    """Ищет ссылки на подстраницы ОЛРР/ОВЛРР."""
    hrefs = set(re.findall(
        r'href="(/page/[Ii]ndex/(?:olrr|ovlrr|centr-licenz|czlrr|otdel-licenzionn)[^"?]*)"',
        html,
    ))
    # Собираем абсолютные URL.
    base_root = re.match(r"^(https?://[^/]+)", base_url).group(1)
    return sorted({f"{base_root}{h}" for h in hrefs})


def parse_block(chunk: str) -> dict | None:
    """Из chunk-а текста (начинается с маркера ЛРР) вытаскивает name/address/phone."""
    # Имя — первая непустая строка (до переноса)
    first_newline = chunk.find("\n")
    first_line = (chunk[:first_newline] if first_newline > 0 else chunk[:200]).strip()
    # Убираем ведущие номера списка ("2.", "10.")
    name = re.sub(r"^[\s\d\.\-]+", "", first_line).strip()
    # Обрезаем хвосты вида "по вопросам...", "приём..."
    name = name[:200]
    if not name or len(name) < 4:
        return None
    if not UNIT_RE.search(name):
        return None

    # Телефон — первое вхождение в chunk
    phone = ""
    pm = PHONE_RE.search(chunk)
    if pm:
        phone = pm.group(0).strip()

    # Адрес — строка с "ул.|пр.|шоссе|наб.|д. N"
    addr = ""
    for ln in chunk.splitlines():
        ln = ln.strip()
        if not ln or len(ln) < 10:
            continue
        if re.search(r"\b(ул\.?\b|ул\s|пр-?кт|проспект|пер\.?\b|переулок|ш\.?\b|шоссе|наб\.?\b|д\.\s*\d|дом\s+\d|мкр|корп\.)", ln):
            # исключаем строки-расписания
            if re.search(r"^(понедельник|вторник|среда|четверг|пятница|суббота|1-я|2-я|3-я|4-я)", ln, re.I):
                continue
            addr = ln[:300]
            break

    return {
        "name": name,
        "address": addr,
        "phone": phone,
    }


# Маркёр начала подразделения (учитываем ведущий номер "2.", "10.")
SECTION_MARKER = re.compile(
    r"(?:^|[\s>])(?P<num>\d{1,2}\.?\s*)?"
    r"(?P<kind>ЦЛРР|ОВЛРР|ОЛРР|Отдел(?:ение)?\s+лицензионн[а-я]{0,6}|Центр\s+лицензионно[\-а-я]{0,15}\s+работ[а-я]{0,3})",
    re.IGNORECASE,
)

# Для табличных страниц — добавляем "Офис приема"
UNIT_NAME_RE = re.compile(
    r"(ОЛРР|ЦЛРР|ОВЛРР|Отдел(?:ение)?\s+лицензионн|Центр\s+лицензионно|Офис\s+при[её]ма)",
    re.IGNORECASE,
)


def parse_table(html: str) -> list[dict]:
    """Табличный fallback для страниц вроде 34.rosguard.gov.ru.

    Строки таблицы имеют ячейки с названием, адресом и телефоном в
    разном порядке, поэтому каждую ячейку классифицируем по содержимому.
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
    records = []
    for row in rows:
        cells_raw = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL | re.IGNORECASE)
        cells = []
        for c in cells_raw:
            t = strip_tags(c).strip()
            t = re.sub(r"\s+", " ", t)
            if t:
                cells.append(t)
        if not cells:
            continue

        name = addr = phone = ""
        for c in cells:
            if not name and UNIT_NAME_RE.search(c):
                name = c[:200]
            if not addr and re.search(r"\d{6}[,\s]", c) and re.search(r"\b(ул\.?|пр-?кт|проспект|пер\.?|наб\.?|шоссе|ш\.|д\.\s*\d|дом\s*\d|город|г\.)", c):
                addr = c[:300]
            if not phone:
                pm = PHONE_RE.search(c)
                if pm:
                    phone = pm.group(0).strip()
        if not name:
            continue
        # отсекаем заголовочные строки ("Наименование", "Адрес")
        if len(name) < 10:
            continue
        records.append({"name": name, "address": addr, "phone": phone})

    # Дедуп.
    seen = set()
    uniq = []
    for r in records:
        k = re.sub(r"\s+", " ", r["name"].lower()).strip()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def parse_page(html: str) -> list[dict]:
    """Парсит страницу в список подразделений.

    HTML у рег. сайтов Росгвардии невалидный (несбалансированные <b>),
    поэтому работаем с plain-текстом после strip_tags. Режем текст по
    маркерам ОЛРР/ЦЛРР/ОВЛРР/Отдел лицензионн/Центр лицензионно.
    """
    text = strip_tags(html)
    # Нормализуем: двоеточия, переносы
    text = re.sub(r"[  ]+", " ", text)

    positions = [m.start("kind") for m in SECTION_MARKER.finditer(text)]
    if not positions:
        return []

    chunks = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        chunks.append(text[start:end])

    records = []
    for c in chunks:
        rec = parse_block(c)
        if rec:
            records.append(rec)

    # Дедуп.
    seen = set()
    uniq = []
    for r in records:
        k = re.sub(r"\s+", " ", r["name"].lower()).strip()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def parse_subpage(html: str) -> dict | None:
    """Парсит индивидуальную страницу ОЛРР/ЦЛРР.

    Берём h1 с ЛРР-маркёром (часто это не первый h1 — первый h1 — шапка сайта).
    Адрес/телефон — из body после h1.
    """
    name = ""
    name_end = 0
    for m in re.finditer(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE):
        t = strip_tags(m.group(1)).strip()
        if UNIT_RE.search(t):
            name = t
            name_end = m.end()
            break
    if not name:
        # fallback: <title>
        m = re.search(r"<title>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        if m:
            title = strip_tags(m.group(1)).split("–")[0].strip()
            if UNIT_RE.search(title):
                name = title

    if not name:
        return None

    # Текст от h1 до конца — там и адрес, и телефон.
    body_html = html[name_end:] if name_end else html
    text = strip_tags(body_html)
    phone = ""
    pm = PHONE_RE.search(text)
    if pm:
        phone = pm.group(0).strip()

    addr = ""
    for ln in text.splitlines():
        ln = ln.strip()
        if len(ln) < 10:
            continue
        if re.search(r"\b(ул\.?\b|ул\s|пр-?кт|проспект|пер\.?\b|переулок|ш\.?\b|шоссе|наб\.?\b|д\.\s*\d|дом\s+\d|мкр|корп\.)", ln):
            if re.search(r"^(понедельник|вторник|среда|четверг|пятница|суббота|1-я|2-я|3-я|4-я)", ln, re.I):
                continue
            addr = ln[:300]
            break

    return {"name": name, "address": addr, "phone": phone}


def dadata_suggest(query: str, token: str, count: int = 3) -> list[dict]:
    try:
        r = requests.post(
            SUGGEST_URL,
            json={"query": query, "count": count, "type": "LEGAL"},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {token}",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("suggestions") or []
    except Exception:
        return []


class Command(BaseCommand):
    help = "Импорт территориальных подразделений ЛРР Росгвардии"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--only", default="", help="Только один регион по number (например --only 77)")
        parser.add_argument("--no-dadata", action="store_true")
        parser.add_argument("--sleep", type=float, default=0.3)

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        only = opts["only"]
        no_dadata = opts["no_dadata"]
        sleep_s = opts["sleep"]

        kind = LegalEntityKind.objects.filter(short_name="ЛРР").first()
        if not kind and not dry_run:
            self.stdout.write(self.style.ERROR(
                "Нет LegalEntityKind(ЛРР). Прогоните миграцию 0034."
            ))
            return

        token = getattr(settings, "DADATA_API_KEY", "") or ""
        if not token and not no_dadata:
            self.stdout.write(self.style.WARNING(
                "DADATA_API_KEY не задан — обогащение выключено."
            ))
            no_dadata = True

        # Перебираем все регионы из справочника.
        qs = Region.objects.order_by("number")
        if only:
            qs = qs.filter(number=int(only))

        total_created = total_updated = total_failed = 0

        for region in qs:
            subdomain = f"{region.number:02d}"
            self.stdout.write(f"\n=== {region.number:3} {region.name[:45]}  →  {subdomain}.rosguard.gov.ru ===")

            urls = discover_lrr_pages(subdomain)
            if not urls:
                self.stdout.write(self.style.WARNING("  страница ЛРР не найдена"))
                total_failed += 1
                time.sleep(sleep_s)
                continue

            records = []
            used_url = None
            for url in urls[:3]:  # пробуем 3 самых специфичных кандидата
                html = fetch(url)
                if not html:
                    continue
                used_url = url

                # Hub-mode: много ссылок на отдельные ОЛРР-страницы.
                subpage_urls = find_subpage_links(html, url)
                if len(subpage_urls) >= 5:
                    self.stdout.write(f"  hub-страница: {url} ({len(subpage_urls)} подстраниц)")
                    for sp in subpage_urls:
                        sp_html = fetch(sp)
                        if not sp_html:
                            continue
                        rec = parse_subpage(sp_html)
                        if rec:
                            rec["source_url"] = sp
                            records.append(rec)
                        time.sleep(0.2)
                    if records:
                        break
                    continue

                # Inline + табличный парсер
                recs = parse_page(html)
                if not recs:
                    recs = parse_table(html)
                    if recs:
                        self.stdout.write(f"  табличный режим: {url} ({len(recs)} записей)")
                else:
                    self.stdout.write(f"  inline-режим: {url} ({len(recs)} записей)")
                if recs:
                    records.extend(recs)
                    # Если есть подстраницы — добавим тоже.
                    for sp in subpage_urls:
                        sp_html = fetch(sp)
                        if not sp_html:
                            continue
                        rec = parse_subpage(sp_html)
                        if rec:
                            rec["source_url"] = sp
                            records.append(rec)
                        time.sleep(0.2)
                    break

            # Если ни один URL не дал записей — HQ-fallback с главной.
            if not records:
                hq = parse_hq_from_home(subdomain, region.name)
                if hq:
                    records.append(hq)
                    self.stdout.write(f"  HQ-fallback: 1 запись")

            # Защита от коллизий ключа (short_name, kind, region):
            # если встречается одинаковое имя в пределах региона с разными
            # адресами — добавляем хвост адреса к имени.
            name_groups: dict[str, list[dict]] = {}
            for r in records:
                name_groups.setdefault(r["name"], []).append(r)
            for nm, group in name_groups.items():
                if len(group) > 1:
                    for r in group:
                        tail = (r.get("address") or "")[:60].strip(" ,")
                        if tail:
                            r["name"] = f"{nm} — {tail}"

            # Дедуп.
            seen = set()
            uniq = []
            for r in records:
                k = re.sub(r"\s+", " ", r["name"].lower()).strip()
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(r)
            records = uniq

            self.stdout.write(f"  записей: {len(records)}")

            # Сохраняем.
            created = updated = 0
            with transaction.atomic():
                for rec in records:
                    inn = ""
                    ogrn = ""
                    director_name = ""
                    director_title = ""
                    email = ""
                    legal_address = rec.get("address") or ""
                    full_name = rec["name"]

                    # Автоподставим "Управление Росгвардии по <регион>" к короткому имени.
                    if not re.search(r"росгвард", full_name, re.I):
                        full_name = f"{full_name} (Управление Росгвардии по {region.name})"

                    # DaData suggest по исходному названию.
                    if not no_dadata:
                        for q in [rec["name"], f"{rec['name']} Росгвардия {region.name}"]:
                            sug = dadata_suggest(q, token, count=1)
                            if sug:
                                data = sug[0].get("data") or {}
                                inn = data.get("inn") or inn
                                ogrn = data.get("ogrn") or ogrn
                                if not legal_address:
                                    addr = (data.get("address") or {})
                                    legal_address = addr.get("unrestricted_value") or ""
                                mgmt = data.get("management") or {}
                                director_name = director_name or mgmt.get("name", "")
                                director_title = director_title or mgmt.get("post", "")
                                email = email or data.get("emails", "") or ""
                                break
                            time.sleep(0.1)

                    # Нормализуем регион: если в адресе явно определили —
                    # используем его, иначе берём текущий region.
                    rgn_num = find_region_number(legal_address) or region.number
                    rgn = Region.objects.filter(number=rgn_num).first() or region

                    # Уникальность — по (name, region).
                    defaults = {
                        "name": full_name[:500],
                        "short_name": rec["name"][:255],
                        "kind": kind,
                        "entity_type": "other",
                        "status": "active",
                        "is_active": True,
                        "inn": (inn or "")[:12],
                        "ogrn": (ogrn or "")[:15],
                        "legal_address": legal_address[:5000],
                        "phone": (rec.get("phone") or "")[:20],
                        "email": (email or "")[:100],
                        "director_name": director_name[:255],
                        "director_title": director_title[:255],
                        "region": rgn,
                        "notes": (
                            "Импорт с регионального сайта Росгвардии "
                            f"({subdomain}.rosguard.gov.ru)"
                            + (f"\nИсточник: {rec.get('source_url')}" if rec.get("source_url") else "")
                        ),
                    }

                    if dry_run:
                        self.stdout.write(
                            f"    [dry] {rec['name'][:80]}  |  "
                            f"{legal_address[:60]}  |  {rec.get('phone', '')}"
                        )
                        continue

                    obj, is_new = LegalEntity.objects.update_or_create(
                        short_name=rec["name"][:255], kind=kind, region=rgn,
                        defaults=defaults,
                    )
                    if is_new:
                        created += 1
                    else:
                        updated += 1

            self.stdout.write(self.style.SUCCESS(
                f"  создано: {created}, обновлено: {updated}"
            ))
            total_created += created
            total_updated += updated
            time.sleep(sleep_s)

        self.stdout.write(self.style.SUCCESS(
            f"\nИТОГО: создано {total_created}, обновлено {total_updated}, "
            f"регионов с ошибкой: {total_failed}"
        ))
