"""Импорт районных/городских судов общей юрисдикции в crm.LegalEntity.

Seed-источник — список court_id с GitHub `dataout-org/sudrfparser`
(courts_info/sudrf_websites.json, ~2300 СОЮ, MIT-лицензия). В нём имена
+ URL сайтов, но без адресов.

Адреса добираем через DaData /suggest/court — берём первый match
с совпадающим court_id (поле `data.code`).

Идемпотентно по LegalEntity.court_code (формат «22RS0001»). При повторном
запуске запись обновляется.

  python manage.py import_district_courts --limit 100   # тест
  python manage.py import_district_courts --dry-run
  python manage.py import_district_courts               # полный
"""
import json
import os
import time
from collections import Counter

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind, Region


GITHUB_URL = (
    "https://raw.githubusercontent.com/dataout-org/sudrfparser/main/"
    "courts_info/sudrf_websites.json"
)
DADATA_SUGGEST_URL = (
    "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/court"
)

# Типы (2 буквы внутри court_id) которые мы импортируем.
INCLUDE_TYPES = {"RS"}  # районный/городской/межрайонный

# Регекс для типа суда в имени → маркер kind. Сейчас все RS = «Районный суд».
KIND_BY_TYPE = {"RS": "Районный суд"}

# Алиасы судебных кодов регионов → Region.number в нашей БД.
# Судебная нумерация местами расходится с классификатором субъектов РФ:
#   20 → Чечня (по классификатору 95)
#   91 → Крым (по классификатору 82)
#   88 → Эвенкийский АО (упразднён, входит в Красноярский край 24)
REGION_ALIASES = {
    20: 95,
    91: 82,
    88: 24,
}


def _dadata_find_court(query, api_key, secret_key, count=10):
    """DaData /suggest/court — top-N по name."""
    try:
        r = requests.post(
            DADATA_SUGGEST_URL,
            json={"query": query, "count": count},
            headers={
                "Authorization": f"Token {api_key}",
                "X-Secret": secret_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        return (r.json() or {}).get("suggestions") or []
    except Exception:
        return []


def _clean_name(s: str) -> str:
    """Из 'Алейский городской суд (Алтайский край)' → 'Алейский городской суд'."""
    if not s:
        return ""
    # Убираем суффикс «(регион)»
    return s.split("(")[0].strip()


class Command(BaseCommand):
    help = "Импорт районных/городских судов в LegalEntity (GitHub seed + DaData)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                            help="Ограничить число записей (для теста).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Не сохранять — только посчитать.")
        parser.add_argument("--no-dadata", action="store_true",
                            help="Не использовать DaData (без адресов).")

    def _fetch_seed(self):
        r = requests.get(GITHUB_URL, timeout=60)
        r.raise_for_status()
        data = r.json()
        out = []
        for region_code, courts in data.items():
            for c in courts:
                cid = c.get("court_id") or ""
                if len(cid) >= 4 and cid[2:4] in INCLUDE_TYPES:
                    out.append({
                        "court_id": cid,
                        "region_code": cid[:2],  # 2 цифры = код субъекта
                        "name": c.get("court_name") or "",
                        "website": c.get("court_website") or "",
                        "type_code": cid[2:4],
                    })
        return out

    def handle(self, *args, **opts):
        limit = opts["limit"]
        dry = opts["dry_run"]
        use_dadata = not opts["no_dadata"]

        api_key = getattr(settings, "DADATA_API_KEY", "") or os.environ.get("DADATA_API_KEY", "")
        secret_key = getattr(settings, "DADATA_SECRET_KEY", "") or os.environ.get("DADATA_SECRET_KEY", "")
        if use_dadata and (not api_key or not secret_key):
            self.stderr.write("DADATA_API_KEY/DADATA_SECRET_KEY не заданы. "
                              "Используй --no-dadata если хочешь без адресов.")
            return

        # Kind = «Районный суд»
        kind_district = LegalEntityKind.objects.filter(short_name="Районный суд").first()
        if kind_district is None:
            self.stderr.write("LegalEntityKind «Районный суд» не найден — нужна миграция 0092")
            return
        self.stdout.write(f"Используем kind: {kind_district}")

        # Регионы → словарь {number: Region}
        regions_by_number = {rg.number: rg for rg in Region.objects.all()}

        self.stdout.write(f"Скачиваем GitHub-seed: {GITHUB_URL}")
        seed = self._fetch_seed()
        self.stdout.write(f"  Получено: {len(seed)} судов типов {INCLUDE_TYPES}")

        stats = Counter()
        for i, c in enumerate(seed):
            if limit and i >= limit:
                break

            cid = c["court_id"]
            name_clean = _clean_name(c["name"])
            try:
                region_num = int(c["region_code"])
            except (ValueError, TypeError):
                region_num = None
            # Применяем алиас если судебный код расходится с классификатором
            mapped_num = REGION_ALIASES.get(region_num, region_num)
            region = regions_by_number.get(mapped_num) if mapped_num else None
            if region is None:
                stats["no_region"] += 1

            # DaData: ищем суд по имени, выбираем match по court_id.
            address = ""
            legal_address = ""
            inn = ""
            full_name = c["name"]
            short_name = name_clean
            if use_dadata:
                suggestions = _dadata_find_court(name_clean, api_key, secret_key)
                match = None
                for s in suggestions:
                    if (s.get("data") or {}).get("code") == cid:
                        match = s
                        break
                if match is None:
                    stats["dadata_no_match"] += 1
                else:
                    stats["dadata_match"] += 1
                    d = match["data"]
                    address = d.get("address") or ""
                    legal_address = d.get("legal_address") or ""
                    inn = d.get("inn") or ""
                    full_name = d.get("name") or full_name
                    if match.get("value"):
                        short_name = match["value"]
                # DaData free tier — 30 req/sec.
                time.sleep(0.05)

            if dry:
                stats["would_create_or_update"] += 1
                continue

            postal = address or ""
            legal = legal_address or address or ""
            defaults = {
                "name": full_name[:500],
                "short_name": short_name[:255],
                "entity_type": "other",
                "kind": kind_district,
                "region": region,
                "postal_address": postal,
                "legal_address": legal,
                "actual_address": postal or legal,
                "website": (c.get("website") or "")[:200],
                "inn": inn[:12] if inn else "",
                "is_active": True,
            }
            try:
                _, created = LegalEntity.objects.update_or_create(
                    court_code=cid, defaults=defaults,
                )
                stats["created" if created else "updated"] += 1
            except Exception as exc:
                stats["error"] += 1
                if stats["error"] <= 3:
                    self.stderr.write(f"  ! err on {cid}: {exc}")

            if (i + 1) % 100 == 0:
                self.stdout.write(
                    f"  …обработано {i+1}: created={stats['created']} "
                    f"updated={stats['updated']} "
                    f"dadata_match={stats['dadata_match']} "
                    f"dadata_no_match={stats['dadata_no_match']}"
                )

        # Отчёт
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== ИТОГ ==="))
        for k in ("created", "updated", "would_create_or_update",
                  "no_region", "dadata_match", "dadata_no_match", "error"):
            self.stdout.write(f"  {k}: {stats[k]}")
