"""
Импорт МФО со страницы https://ru.myfin.by/mfo
с обогащением реквизитов через DaData по ИНН.
"""
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind
from apps.crm.management.commands.import_cbr_banks import (
    dadata_find_by_ogrn,
    enrich_from_dadata,
    map_entity_type,
)


MYFIN_URL = "https://ru.myfin.by/mfo"
DADATA_FIND_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.9",
}
# Для пагинированных страниц (2+) сайт требует XHR-заголовок,
# иначе возвращает идентичный первой странице HTML.
HEADERS_AJAX = {**HEADERS, "X-Requested-With": "XMLHttpRequest"}

MFO_ITEM_RE = re.compile(
    r'<div class="mfo-list-item__name">\s*<a href="/mfo/[^"]+"[^>]*>([^<]+)</a>',
    re.DOTALL,
)
INFO_ROW_RE = re.compile(
    r'<div class="mfo-list-item__info-name">\s*([^<:]+):?\s*</div>\s*'
    r'<div class="mfo-list-item__info-data">\s*([^<]+?)\s*</div>',
    re.DOTALL,
)
ITEM_SPLIT_RE = re.compile(r'<div class="mfo-list__item[^"]*"[^>]*data-key="[^"]*">')
TOTAL_COUNT_RE = re.compile(r'data-total-count="(\d+)"')
PAGE_SIZE_RE = re.compile(r'data-page-size="(\d+)"')


def dadata_find_by_inn(query: str) -> dict | None:
    token = getattr(settings, "DADATA_API_KEY", "") or ""
    if not token or not query:
        return None
    try:
        r = requests.post(
            DADATA_FIND_URL,
            json={"query": query, "count": 1},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {token}",
            },
            timeout=15,
        )
        r.raise_for_status()
        sugs = r.json().get("suggestions") or []
        return sugs[0].get("data") if sugs else None
    except Exception:
        return None


def parse_page(html: str) -> list[dict]:
    """Возвращает [{brand, inn, ogrn}, ...] со страницы листинга МФО."""
    chunks = ITEM_SPLIT_RE.split(html)[1:]  # первая часть — до первого элемента
    items = []
    for chunk in chunks:
        m = MFO_ITEM_RE.search(chunk)
        if not m:
            continue
        brand = m.group(1).strip()
        info = dict(INFO_ROW_RE.findall(chunk))
        # Нормализуем ключи: иногда встречается «ИНН», «ОРГН» (опечатка на сайте)
        norm = {k.strip().upper(): v.strip() for k, v in info.items()}
        inn = norm.get("ИНН") or ""
        ogrn = norm.get("ОГРН") or norm.get("ОРГН") or ""
        if not inn and not ogrn:
            continue
        items.append({"brand": brand, "inn": inn, "ogrn": ogrn})
    return items


class Command(BaseCommand):
    help = "Импорт МФО с ru.myfin.by/mfo с обогащением через DaData."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                            help="Максимум МФО (0 = все)")
        parser.add_argument("--max-pages", type=int, default=25,
                            help="Максимум страниц листинга (default 25)")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-enrich", action="store_true",
                            help="Пропустить DaData")
        parser.add_argument("--sleep", type=float, default=0.3,
                            help="Пауза между страницами листинга, сек")

    def fetch(self, url: str, ajax: bool = False) -> str:
        r = requests.get(url, headers=HEADERS_AJAX if ajax else HEADERS, timeout=30)
        r.raise_for_status()
        return r.text

    def handle(self, *args, **opts):
        limit = opts["limit"]
        max_pages = opts["max_pages"]
        dry_run = opts["dry_run"]
        no_enrich = opts["no_enrich"]
        sleep_s = opts["sleep"]

        mfo_kind = LegalEntityKind.objects.filter(short_name="МФО").first()
        if not mfo_kind and not dry_run:
            self.stdout.write(self.style.ERROR(
                "В справочнике нет записи МФО — запусти миграцию 0027."
            ))
            return

        self.stdout.write(f"Загружаю {MYFIN_URL} …")
        html = self.fetch(MYFIN_URL)

        total_m = TOTAL_COUNT_RE.search(html)
        page_size_m = PAGE_SIZE_RE.search(html)
        total = int(total_m.group(1)) if total_m else 0
        page_size = int(page_size_m.group(1)) if page_size_m else 40
        pages = (total + page_size - 1) // page_size if total else 1
        pages = min(pages, max_pages)
        self.stdout.write(f"Всего МФО ≈ {total}, страниц: {pages}, по {page_size}")

        # 1) собрать всё с листинга
        all_items: list[dict] = parse_page(html)
        for p in range(2, pages + 1):
            url = f"{MYFIN_URL}?dp-1-page={p}&dp-1-per-page={page_size}"
            self.stdout.write(f"  стр.{p}: {url}")
            try:
                page_html = self.fetch(url, ajax=True)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"    ошибка: {e}"))
                continue
            items = parse_page(page_html)
            all_items.extend(items)
            if sleep_s:
                time.sleep(sleep_s)

        # Убираем дубликаты по ИНН
        seen = set()
        uniq = []
        for it in all_items:
            key = it["inn"] or it["ogrn"]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        if limit:
            uniq = uniq[:limit]
        self.stdout.write(f"Собрано уникальных МФО: {len(uniq)}")

        # 2) обогащение + запись
        created = 0
        updated = 0
        skipped = 0
        for idx, it in enumerate(uniq, 1):
            brand = it["brand"]
            inn = it["inn"]
            ogrn = it["ogrn"]

            dd = None
            if not no_enrich:
                dd = dadata_find_by_inn(inn or ogrn)
                if not dd and inn:
                    # попробуем по ОГРН
                    dd = dadata_find_by_ogrn(ogrn) if ogrn else None

            enrichment = enrich_from_dadata(dd) if dd else {}
            if not dd:
                skipped_note = " (DaData пусто)"
            else:
                skipped_note = ""

            # Формируем поля
            name = enrichment.get("name") or brand
            defaults = {
                "name": name,
                "brand": brand,
                "status": "active",
                "is_active": True,
                "notes": (
                    f"Импорт с ru.myfin.by/mfo\n"
                    f"Бренд: {brand}\n"
                    f"ИНН: {inn or '—'}\n"
                    f"ОГРН: {ogrn or '—'}"
                ),
            }
            if mfo_kind:
                defaults["kind"] = mfo_kind
            for k, v in enrichment.items():
                if v:
                    defaults[k] = v
            # перекрываем entity_type по ОПФ (если из DaData не пришёл)
            opf = ((dd or {}).get("opf") or {}).get("short") if dd else ""
            if opf:
                defaults["entity_type"] = map_entity_type(opf)

            self.stdout.write(
                f"  [{idx}] {brand} | ИНН={inn} ОГРН={ogrn} → "
                f"{defaults.get('name','—')[:50]}{skipped_note}"
            )

            if dry_run:
                continue

            # ключ — по ИНН (если есть), иначе по ОГРН
            lookup = {"inn": inn} if inn else {"ogrn": ogrn}
            defaults.setdefault("inn", inn)
            defaults.setdefault("ogrn", ogrn)
            obj, is_new = LegalEntity.objects.update_or_create(
                **lookup, defaults=defaults,
            )
            if is_new:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано: {created}, обновлено: {updated}"
        ))
