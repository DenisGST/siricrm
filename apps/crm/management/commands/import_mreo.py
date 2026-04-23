"""
Импорт подразделений МРЭО / ОГИБДД России из агрегатора onlinegibdd.ru.

Алгоритм:
  1) Скачиваем https://onlinegibdd.ru/sitemap_gibdd.xml, берём все ?id=N
  2) Для каждого id скачиваем /struktura-gibdd/?id=N (cp1251),
     парсим название, код подразделения, адрес, телефон, часы приёма.
  3) Создаём LegalEntity с kind=МРЭО.

МРЭО не имеют самостоятельных ИНН — это подразделения ГУ МВД по субъектам РФ.
Для всех используется ИНН центрального аппарата МВД РФ (1770700193… — не публикуется
в единообразном виде). Для уникальности объектов используем пару (name, okpo),
где okpo — код подразделения из онлайнГибдд.
Руководитель оставляется пустым.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind


SITEMAP_URL = "https://onlinegibdd.ru/sitemap_gibdd.xml"
DETAIL_URL = "https://onlinegibdd.ru/struktura-gibdd/?id={id}"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}

ID_RE = re.compile(r"/struktura-gibdd/\?id=(\d+)")
H1_RE = re.compile(r"<h1[^>]*>([^<]+)</h1>")
CODE_RE = re.compile(r"<span class=\"grey-d-2\">Код</span>\s*(\d+)")
FIELD_RE = re.compile(
    r'<div class="w-138 grey-d-2 mr-6">\s*([^<]+?)\s*</div>\s*'
    r'<div[^>]*>(.*?)</div>',
    re.DOTALL,
)


def fetch(url: str, timeout: int = 15) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    # Site serves cp1251
    r.encoding = "cp1251"
    return r.text


def parse_detail(html: str) -> dict | None:
    name_m = H1_RE.search(html)
    if not name_m:
        return None
    name = re.sub(r"\s+", " ", name_m.group(1)).strip()
    code = ""
    m = CODE_RE.search(html)
    if m:
        code = m.group(1).strip()

    address = phone = hours = region = ""
    for field_m in FIELD_RE.finditer(html):
        label = field_m.group(1).strip().rstrip(":").lower()
        raw_val = field_m.group(2)
        val = re.sub(r"<[^>]+>", " ", raw_val)
        val = re.sub(r"\s+", " ", val).strip()
        if "адрес" in label:
            address = val
        elif "телефон" in label:
            phone = val
        elif "часы" in label or "режим" in label:
            hours = val
        elif "регион" in label:
            region = val
    return {
        "name": name,
        "code": code,
        "address": address,
        "phone": phone,
        "hours": hours,
        "region": region,
    }


class Command(BaseCommand):
    help = "Импорт МРЭО / ОГИБДД России из onlinegibdd.ru"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--workers", type=int, default=10,
                            help="Параллельных запросов (default 10)")

    def handle(self, *args, **opts):
        limit = opts["limit"]
        dry_run = opts["dry_run"]
        workers = opts["workers"]

        kind = LegalEntityKind.objects.filter(short_name="МРЭО").first()
        if not kind and not dry_run:
            self.stdout.write(self.style.ERROR(
                "Нет LegalEntityKind(МРЭО). Прогоните миграцию 0030."
            ))
            return

        # 1) sitemap
        self.stdout.write(f"Загружаю sitemap: {SITEMAP_URL} …")
        sm = fetch(SITEMAP_URL)
        ids = sorted(set(int(m) for m in ID_RE.findall(sm)))
        if limit:
            ids = ids[:limit]
        self.stdout.write(f"  ID в sitemap: {len(ids)}")

        # 2) параллельный парсинг страниц
        results: list[dict] = []
        failures = 0

        def job(i: int):
            try:
                return i, parse_detail(fetch(DETAIL_URL.format(id=i)))
            except Exception:
                return i, None

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(job, i) for i in ids]
            for n, fut in enumerate(as_completed(futures), 1):
                _id, data = fut.result()
                if data and data.get("name"):
                    results.append({**data, "source_id": _id})
                else:
                    failures += 1
                if n % 100 == 0:
                    self.stdout.write(f"  обработано: {n}/{len(ids)} (ошибок {failures})")

        self.stdout.write(f"  успешно распарсено: {len(results)}, ошибок: {failures}")

        # 3) запись в БД
        created = updated = skipped = 0
        for r in results:
            name = r["name"]
            if not name:
                skipped += 1
                continue
            notes = (
                f"Импорт из onlinegibdd.ru\n"
                f"Источник id: {r['source_id']}\n"
                f"Код подразделения: {r['code'] or '—'}\n"
                f"Регион: {r['region'] or '—'}\n"
                f"Режим работы: {r['hours'] or '—'}"
            )
            defaults = {
                "name": name,
                "short_name": name[:255],
                "entity_type": "other",
                "kind": kind,
                "status": "active",
                "is_active": True,
                "okpo": (r["code"] or "")[:14],
                "legal_address": r["address"] or "",
                "phone": (r["phone"] or "")[:20],
                "notes": notes,
                "director_name": "",
                "director_title": "",
            }

            if dry_run:
                self.stdout.write(
                    f"  [{r['source_id']}] {name[:70]} | код={r['code']} | {r['phone']}"
                )
                continue

            # Уникальность: (name, okpo). Код подразделения у всех есть и уникален.
            obj, is_new = LegalEntity.objects.update_or_create(
                name=name, okpo=(r["code"] or "")[:14],
                defaults=defaults,
            )
            if is_new:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано: {created}, обновлено: {updated}, пропущено: {skipped}"
        ))
