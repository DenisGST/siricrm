"""
Импорт реквизитов депозитного счёта арбитражных судов субъектов РФ.

На официальных сайтах судов субъектов (типовой движок arbitr.ru) депозитные
реквизиты публикуются в одном из двух мест:

  1) /process/duty/deposit — страница с кастомным элементом
     <deposit-invoice recipient="..." inn="..." kpp="..." bik="..."
                      account="..." treasury="..." kbk="..." okato="..."
                      bank="...">
     (самый массовый вариант — ~85% судов).

  2) /process/deposit_info, /deposit, /about/deposit — отдельная страница
     с текстом/таблицей. Парсим ключи УФК/ИНН/КПП/БИК/л/с/КБК/ОКТМО/
     счёт казначейства через regex.

Алгоритм:
  - берём JSON arbitr.ru/ac/map (88 судов субъектов);
  - по s_name маппим на Region (через REGION_PATTERNS из
    import_arbitration_courts);
  - пробуем серию кандидатных URL;
  - в каждой странице ищем сначала <deposit-invoice>, затем fallback;
  - собираем многострочный текст и пишем в Region.court_deposit_details.
"""
import html
import re
import requests

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.html import strip_tags

from apps.crm.models import Region
from apps.crm.management.commands.import_arbitration_courts import (
    EXTRA_REGIONS,
    find_region,
)


API_URL = "https://arbitr.ru/ac/map"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru",
}

CANDIDATE_PATHS = [
    "/process/duty/deposit",
    "/process/deposit_info",
    "/deposit",
    "/deposit_form",
    "/deposit_details",
    "/about/deposit",
    "/process/deposit",
]


# Признаки, что данные относятся к госпошлине (ФНС), а не к депозиту суда:
# - ИНН получателя = ИНН ФНС России
# - Текст «Казначейство России» / «ФНС России» в Получателе
# - Казначейский счёт ФНС 03100643000000018500
GOSPOSHLINA_INN = "7727406020"
GOSPOSHLINA_ACCOUNT = "03100643000000018500"


def looks_like_duty_not_deposit(data: dict) -> bool:
    if (data.get("inn") or "").strip() == GOSPOSHLINA_INN:
        return True
    if GOSPOSHLINA_ACCOUNT in (data.get("account") or ""):
        return True
    if GOSPOSHLINA_ACCOUNT in (data.get("treasury") or ""):
        return True
    rec = (data.get("recipient") or "").lower()
    if "казначейство россии" in rec or "(фнс россии)" in rec:
        return True
    return False

DEPOSIT_FIELDS = [
    ("recipient", "Получатель"),
    ("bank", "Банк"),
    ("bik", "БИК"),
    ("account", "Казначейский счёт"),
    ("treasury", "Единый казначейский счёт"),
    ("inn", "ИНН"),
    ("kpp", "КПП"),
    ("kbk", "КБК"),
    ("okato", "ОКТМО"),
    ("codes", "Коды платежа"),
]


def normalize_host(url: str) -> str:
    """http://spb.arbitr.ru/ → https://spb.arbitr.ru (без хвостового слеша)."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    u = u.replace("http://", "https://", 1) if u.startswith("http://") else u
    if not u.startswith("http"):
        u = "https://" + u
    return u


ATTR_RE = re.compile(
    r'([a-zA-Z_][\w\-]*)\s*=\s*"([^"]*)"',
    re.DOTALL,
)


def parse_deposit_invoice(html_text: str) -> dict | None:
    """Ищем <deposit-invoice ...> и извлекаем пары attr=val."""
    m = re.search(r"<deposit-invoice\b([^>]*)>", html_text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    attrs_blob = m.group(1)
    data = {}
    for am in ATTR_RE.finditer(attrs_blob):
        data[am.group(1).lower()] = html.unescape(am.group(2)).strip()
    return data or None


# Fallback-парсер: ищем узнаваемые блоки в визуальном тексте страницы.
# recipient обрывается до следующего ключа реквизитов, иначе захватит всё.
_RECIP_STOP = r"(?=\s*(?:ИНН|КПП|БИК|КБК|ОКТМО|ОКАТО|Единый\s+казначейск|Казначейск\w*\s*сч[её]т|Банк\s+получател|Наименовани\w*\s+банк|Сч[её]т|р/с|В\s+назначении))"
FALLBACK_RE = [
    ("recipient", re.compile(
        r"(УФК\s+по\s+[^\n]*?\([^\)]+\)[^\n]*?)" + _RECIP_STOP, re.IGNORECASE,
    )),
    # Пропускаем скобки типа "(поле 61 платежного поручения)" между меткой и числом.
    ("inn",     re.compile(r"ИНН\s*(?:получател\w*)?\s*(?:\([^)]*\)\s*)?(\d{10})", re.IGNORECASE)),
    ("kpp",     re.compile(r"КПП\s*(?:получател\w*)?\s*(?:\([^)]*\)\s*)?(\d{9})",  re.IGNORECASE)),
    ("bik",     re.compile(r"БИК\s*(?:ТОФК|получател\w*)?\s*(?:\([^)]*\)\s*)?(\d{9})", re.IGNORECASE)),
    # Единый казначейский счёт (ЕКС) = «кор./счет», 40102...
    ("treasury", re.compile(
        r"(?:един\w*\s*казначейск\w*\s*сч[её]т\w*|ЕКС|кор(?:р)?\.?\s*сч[её]т)"
        r"[^\d\(]*(?:\([^)]*\)\s*)?(\d{20})", re.IGNORECASE,
    )),
    # Казначейский счёт = «р/с», 03...
    ("account", re.compile(
        r"(?<!единый\s)(?<!ЕКС\s)(?:казначейск\w*\s*сч[её]т\w*|\bр/с|расч(?:[её]тн\w*)?\s*сч[её]т)"
        r"[^\d\(]*(?:\([^)]*\)\s*)?(\d{20})", re.IGNORECASE,
    )),
    ("kbk",     re.compile(r"КБК\s*(?:\([^)]*\)\s*)?(\d{20})", re.IGNORECASE)),
    ("okato",   re.compile(r"(?:ОКТМО|ОКАТО)\s*(?:\([^)]*\)\s*)?(\d{8,11})")),
    ("bank",    re.compile(
        r"Банк\b\s*(?:получател\w*|ТОФК)?\s*(?:\([^)]*\)\s*)?[:\s]*"
        r"([^<\n]{5,200}?)(?=\s*(?:БИК|ИНН|КПП|КБК|ОКТМО|Единый|Казначейск|В\s+назначении|Опубликовано|$))",
        re.IGNORECASE,
    )),
]


def parse_fallback(html_text: str) -> dict:
    text = strip_tags(html_text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    out: dict = {}
    for key, pat in FALLBACK_RE:
        m = pat.search(text)
        if m:
            val = m.group(1) if m.lastindex else m.group(0)
            out[key] = val.strip(" :—-").strip()
    return out


def format_details(data: dict, source_url: str) -> str:
    """dict → многострочный текст для Region.court_deposit_details.

    Сайты судов используют разные соглашения для атрибутов
    <deposit-invoice>: где-то `account`=40102 (ЕКС), где-то `account`=0321
    (р/с). Проверяем по префиксу и ставим правильный лейбл независимо
    от исходного ключа.
    """
    acc = (data.get("account") or "").strip()
    treas = (data.get("treasury") or "").strip()
    # 40102... → Единый казначейский счёт (ЕКС / корр.счёт).
    # 0321..., 0322..., 0323... → Казначейский счёт (р/с).
    if acc.startswith("40102") and not treas.startswith("40102"):
        data["treasury"], data["account"] = acc, treas
    elif treas.startswith("0321") and not acc.startswith("0321"):
        data["account"], data["treasury"] = treas, acc

    lines = []
    for key, label in DEPOSIT_FIELDS:
        v = (data.get(key) or "").strip()
        if v:
            lines.append(f"{label}: {v}")
    if source_url:
        lines.append(f"Источник: {source_url}")
    return "\n".join(lines)


def fetch(url: str, timeout: int = 20) -> requests.Response | None:
    try:
        return requests.get(
            url, headers=HEADERS, timeout=timeout, verify=False,
            allow_redirects=True,
        )
    except Exception:
        return None


_ANCHOR_RE = re.compile(
    r'<a[^>]+href="([^"]+)"[^>]*>([^<]{0,150})</a>',
    re.IGNORECASE | re.DOTALL,
)


def _abs_url(href: str, base: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return base.rstrip("/") + "/" + href


def find_deposit_links_on_home(home_url: str) -> list[str]:
    """Сканирует главную и возвращает список кандидатов-ссылок
    (по href, по тексту anchor'а), отсортированных по «схожести с депозитом».
    """
    r = fetch(home_url)
    if not r or r.status_code != 200:
        return []
    seen: set[str] = set()
    out: list[tuple[int, str]] = []

    for m in _ANCHOR_RE.finditer(r.text):
        href = m.group(1).strip()
        text = m.group(2).strip().lower()
        # Фильтруем компонованные href'ы с запятыми/пробелами внутри URL.
        if "," in href or " " in href or "\n" in href:
            continue
        if href.startswith("#") or href.startswith("javascript"):
            continue

        score = 0
        hl = href.lower()
        tl = text
        if "deposit" in hl:
            score += 5
            if "info" in hl or "detail" in hl or "form" in hl:
                score += 3
        if "депозит" in tl:
            score += 5
            if "реквизит" in tl or "банков" in tl:
                score += 3
        if "реквизит" in hl or "requisites" in hl:
            score += 3
        if score == 0:
            continue
        absu = _abs_url(href, home_url)
        if absu in seen:
            continue
        seen.add(absu)
        out.append((score, absu))

    out.sort(key=lambda x: -x[0])
    return [u for _, u in out[:5]]


def scrape_court_deposit(base_url: str) -> tuple[str, str] | tuple[None, None]:
    """
    Возвращает (текст_реквизитов, source_url) или (None, None), если ничего
    не нашли.
    """
    base = normalize_host(base_url)
    if not base:
        return None, None

    def _try(url: str) -> tuple[str, str] | tuple[None, None]:
        r = fetch(url)
        if not r or r.status_code != 200:
            return None, None
        inv = parse_deposit_invoice(r.text)
        if inv and not looks_like_duty_not_deposit(inv):
            return format_details(inv, url), url
        if "депозит" in r.text.lower():
            fb = parse_fallback(r.text)
            if len(fb) >= 3 and not looks_like_duty_not_deposit(fb):
                return format_details(fb, url), url
        return None, None

    # Первый проход — канонические пути.
    for path in CANDIDATE_PATHS:
        details, source = _try(base + path)
        if details:
            return details, source

    # Второй проход — кандидаты-ссылки с главной (по href и тексту).
    for link in find_deposit_links_on_home(base):
        details, source = _try(link)
        if details:
            return details, source

    # Третий проход — посмотреть в разделе «Госпошлина и депозит» /process/duty/
    # и следовать внутренним ссылкам с «депозит» / «реквизит».
    hub = fetch(base + "/process/duty/")
    if hub and hub.status_code == 200:
        candidates: list[str] = []
        for m in _ANCHOR_RE.finditer(hub.text):
            href = m.group(1).strip()
            text = m.group(2).strip().lower()
            if "," in href or " " in href or "\n" in href:
                continue
            if ("депозит" in text) or ("deposit" in href.lower()):
                candidates.append(_abs_url(href, base))
        # уникальные, до 6 кандидатов
        seen: set[str] = set()
        for link in candidates:
            if link in seen:
                continue
            seen.add(link)
            if len(seen) > 6:
                break
            details, source = _try(link)
            if details:
                return details, source

    return None, None


class Command(BaseCommand):
    help = "Импорт реквизитов депозитных счетов арбитражных судов субъектов РФ"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--only-empty", action="store_true",
            help="Только регионы с пустым court_deposit_details",
        )
        parser.add_argument(
            "--region", type=int, default=None,
            help="Обработать только один регион (Region.number)",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        only_empty = opts["only_empty"]
        one_region = opts["region"]

        self.stdout.write(f"Скачиваю {API_URL} …")
        r = requests.get(API_URL, headers=HEADERS, timeout=30, verify=False)
        r.raise_for_status()
        data = r.json()

        subject_courts = []
        for okrug in data:
            for sub in (okrug.get("ac1") or []):
                subject_courts.append(sub)
        self.stdout.write(f"  судов субъектов: {len(subject_courts)}")

        regions_by_num = {r.number: r for r in Region.objects.all()}

        ok = failed = skipped = 0
        failures: list[str] = []

        for court in subject_courts:
            s_name = court.get("s_name") or ""
            name = court.get("name") or ""
            url = court.get("url") or court.get("www") or ""
            num = find_region(s_name) or find_region(name)
            region = regions_by_num.get(num) if num else None
            if not region:
                skipped += 1
                continue

            if one_region and region.number != one_region:
                continue

            target_numbers = [region.number] + EXTRA_REGIONS.get(region.number, [])

            if only_empty:
                if all(
                    (regions_by_num.get(tn) and regions_by_num[tn].court_deposit_details)
                    for tn in target_numbers
                ):
                    continue

            if not url:
                failures.append(f"{region.number:>3} {region.name} — нет URL в JSON")
                failed += 1
                continue

            self.stdout.write(f"  {region.number:>3} {region.name[:30]:30} {url}")
            details_text, source = scrape_court_deposit(url)
            if not details_text:
                failures.append(f"{region.number:>3} {region.name} — не распарсил ({url})")
                failed += 1
                continue

            if dry_run:
                self.stdout.write(
                    f"      → {source}\n      " +
                    details_text.replace("\n", "\n      ")[:500]
                )
                ok += 1
                continue

            with transaction.atomic():
                for tn in target_numbers:
                    tr = regions_by_num.get(tn)
                    if not tr:
                        continue
                    tr.court_deposit_details = details_text
                    tr.save(update_fields=["court_deposit_details"])
            ok += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nИтого: обновлено {ok}, не распарсено {failed}, "
            f"пропущено (не сопоставлено) {skipped}"
        ))
        if failures:
            self.stdout.write("\nНе получилось:")
            for f in failures:
                self.stdout.write(f"  {f}")
