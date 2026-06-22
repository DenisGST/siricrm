"""Импорт мировых судебных участков в crm.LegalEntity (kind=Мировой участок).

Источник — DaData `/findById/court` по предсказанным кодам формата
«{NN}MS{NNNN}» где NN — судебный код субъекта РФ (для большинства совпадает
с классификатором, но есть алиасы — 20→Чечня, 91→Крым, 88→Эвенк).

Алгоритм:
  1. Идём по списку регионов (Region.number) с учётом обратных алиасов
     (для Чечни ищем 20*, для Крыма — 91*, и т. п.).
  2. Для каждого региона перебираем коды 0001…9999 с findById.
  3. Останавливаемся после N подряд пустых ответов (порог настроен на 30).
  4. Идемпотентно через LegalEntity.court_code.

  python manage.py import_magistrate_courts --limit-region 100  # тест
  python manage.py import_magistrate_courts --regions 39,77      # выборочно
  python manage.py import_magistrate_courts                       # полный (всех)
"""
import os
import time
from collections import Counter

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind, Region


FIND_BY_ID_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/court"

# Обратные алиасы: судебный код → Region.number в Siri (см. import_district_courts).
JUDICIAL_PREFIXES = {
    # Чечня: судебные коды 20, классификатор 95.
    95: 20,
    # Крым: судебные коды 91, классификатор 82.
    82: 91,
    # Красноярский край: судебные коды могут быть 24 и 88 (Эвенк).
    # Для мировых обычно 24, оставляем дефолт.
}

# Сколько подряд «пустых» findById допускаем перед стопом по региону.
EMPTY_THRESHOLD = 30


def _find_by_id(code, api_key):
    try:
        r = requests.post(
            FIND_BY_ID_URL,
            json={"query": code},
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        s = (r.json() or {}).get("suggestions") or []
        return s[0]["data"] if s else None
    except Exception:
        return None


class Command(BaseCommand):
    help = "Импорт мировых судебных участков в LegalEntity через DaData findById/court."

    def add_arguments(self, parser):
        parser.add_argument("--regions", type=str, default="",
                            help="Список Region.number через запятую (для теста).")
        parser.add_argument("--limit-region", type=int, default=0,
                            help="Макс. число попыток на регион (для теста).")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--threshold", type=int, default=EMPTY_THRESHOLD,
                            help="Сколько подряд пустых = стоп региона.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        limit_region = opts["limit_region"]
        threshold = opts["threshold"]
        regions_filter = []
        if opts["regions"]:
            try:
                regions_filter = [int(x) for x in opts["regions"].split(",") if x.strip()]
            except ValueError:
                self.stderr.write("--regions: ожидался список целых чисел")
                return

        api_key = (getattr(settings, "DADATA_API_KEY", "")
                   or os.environ.get("DADATA_API_KEY", ""))
        if not api_key:
            self.stderr.write("DADATA_API_KEY не задан")
            return

        kind = LegalEntityKind.objects.filter(short_name="Мировой участок").first()
        if kind is None:
            self.stderr.write("LegalEntityKind «Мировой участок» не найден (нужна миграция 0092)")
            return
        self.stdout.write(f"Используем kind: {kind}")

        # Список регионов: либо явно из --regions, либо все из Region.
        if regions_filter:
            regions = list(Region.objects.filter(number__in=regions_filter).order_by("number"))
        else:
            regions = list(Region.objects.order_by("number"))
        self.stdout.write(f"Регионов для обхода: {len(regions)}")

        stats = Counter()
        for region in regions:
            # Какой судебный префикс брать? По умолчанию — Region.number,
            # но для Чечни (95→20) и Крыма (82→91) перевешиваем.
            judicial_num = JUDICIAL_PREFIXES.get(region.number, region.number)
            prefix = f"{judicial_num:02d}MS"
            self.stdout.write(f"  Region {region.number} «{region.name}» → префикс {prefix}")

            empty_streak = 0
            tried = 0
            found_in_region = 0
            for n in range(1, 10000):
                if limit_region and tried >= limit_region:
                    break
                tried += 1
                code = f"{prefix}{n:04d}"

                # Если уже есть в БД — пропускаем DaData-запрос для идемпотентности
                existing = LegalEntity.objects.filter(court_code=code).only("id").first()
                if existing is None:
                    data = _find_by_id(code, api_key)
                    time.sleep(0.05)
                else:
                    data = {"code": code, "name": existing.name,
                            "address": existing.postal_address,
                            "website": existing.website}

                if not data:
                    empty_streak += 1
                    if empty_streak >= threshold:
                        break
                    continue

                empty_streak = 0
                found_in_region += 1
                stats["found"] += 1

                if dry:
                    stats["would_create_or_update"] += 1
                    continue

                full_name = data.get("name") or ""
                address = data.get("address") or ""
                website = data.get("website") or ""

                defaults = {
                    "name": full_name[:500],
                    "short_name": full_name[:255],
                    "entity_type": "other",
                    "kind": kind,
                    "region": region,
                    "postal_address": address,
                    "legal_address": address,
                    "actual_address": address,
                    "website": website[:200],
                    "is_active": True,
                }
                try:
                    _, created = LegalEntity.objects.update_or_create(
                        court_code=code, defaults=defaults,
                    )
                    stats["created" if created else "updated"] += 1
                except Exception as exc:
                    stats["error"] += 1
                    if stats["error"] <= 3:
                        self.stderr.write(f"  ! err on {code}: {exc}")

            self.stdout.write(f"    -> найдено {found_in_region} участков "
                              f"(tried {tried}, empty_streak={empty_streak})")

        # Отчёт
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== ИТОГ ==="))
        for k in ("created", "updated", "would_create_or_update",
                  "found", "error"):
            self.stdout.write(f"  {k}: {stats[k]}")
        self.stdout.write(f"\nВсего ОС-кодов в БД: "
                          f"{LegalEntity.objects.exclude(court_code__isnull=True).exclude(court_code='').count()}")
