"""
Импорт подразделений ФНС России в таблицу LegalEntity.

Источники — official open data ФНС:
  https://www.nalog.gov.ru/opendata/7707329152-address/
      «Адреса и платёжные реквизиты инспекций» (~390 территориальных органов)
  https://www.nalog.gov.ru/opendata/7707329152-lowerorganization/
      «Перечень подведомственных организаций» (ФКУ, санатории и т. п.)

Руководители намеренно не импортируются (часто меняются).
DaData не используется.
"""
import csv
import io
import re

import requests
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind


ADDR_CSV_URL = (
    "https://data.nalog.ru/opendata/7707329152-address/"
    "data-20260421-structure-20140714.csv"
)
LOWER_CSV_URL = (
    "https://data.nalog.ru/opendata/7707329152-lowerorganization/"
    "data-20251208-structure-20141118.csv"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ИНН/ОГРН центрального аппарата ФНС России — одинаковы для всех
# территориальных подразделений (они не отдельные юр.лица).
FNS_PARENT_INN = "7707329152"
FNS_PARENT_OGRN = "1047707030513"

OKPO_RE = re.compile(r"Код\s*ОКПО\s*[:\-]?\s*(\d{6,10})")


def clean_addr(raw: str) -> str:
    """У ФНС-ресурса адреса зачастую начинаются с запятой и имеют пустые поля."""
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return ", ".join(parts)


class Command(BaseCommand):
    help = "Импорт подразделений ФНС из открытых данных nalog.ru"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                            help="Ограничить число записей (0 = все)")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--source", choices=["address", "lower", "all"],
                            default="all", help="Какой датасет обработать")

    def fetch_csv(self, url: str) -> list[list[str]]:
        self.stdout.write(f"Загружаю {url} …")
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        text = r.content.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        self.stdout.write(f"  строк (с заголовком): {len(rows)}")
        return rows

    def handle(self, *args, **opts):
        limit = opts["limit"]
        dry_run = opts["dry_run"]
        source = opts["source"]

        kind = LegalEntityKind.objects.filter(short_name="ФНС").first()
        if not kind and not dry_run:
            self.stdout.write(self.style.ERROR(
                "В справочнике LegalEntityKind нет ФНС — проверь миграцию 0027."
            ))
            return

        created = 0
        updated = 0

        # ─── 1. Территориальные органы ──────────────────────────────────────
        if source in ("address", "all"):
            rows = self.fetch_csv(ADDR_CSV_URL)
            data_rows = rows[1:]
            if limit:
                data_rows = data_rows[:limit]

            # Чтобы ИНН не ломал уникальность update_or_create (у всех один),
            # ключом будет комбо (kind + okpo / notes). В нашей модели нет
            # уникального ключа по коду ИФНС — поэтому ищем по name.
            for row in data_rows:
                if len(row) < 3:
                    continue
                code_ifns = (row[0] or "").strip()       # GA — Код ИФНС
                name = (row[1] or "").strip()            # GB — Наименование
                address = clean_addr(row[2] or "")       # G1 — Адрес
                phone = (row[3] or "").strip()           # G2 — Телефон
                extra = (row[4] or "").strip()           # G3 — Доп. информация

                if not name:
                    continue

                okpo_m = OKPO_RE.search(extra)
                okpo = okpo_m.group(1) if okpo_m else ""

                notes = (
                    f"Импорт из open data ФНС (adress)\n"
                    f"Код ИФНС: {code_ifns}\n"
                    f"{extra}"
                ).strip()

                defaults = {
                    "name": name,
                    "short_name": name[:255],
                    "entity_type": "other",
                    "kind": kind,
                    "status": "active",
                    "is_active": True,
                    "inn": FNS_PARENT_INN,
                    "ogrn": FNS_PARENT_OGRN,
                    "okpo": okpo[:14],
                    "legal_address": address,
                    "phone": phone[:20],
                    "notes": notes,
                    "director_name": "",
                    "director_title": "",
                }

                self.stdout.write(
                    f"  [{code_ifns or '-'}] {name[:70]}"
                )

                if dry_run:
                    continue

                # Уникальность по (name, inn) — ИНН у всех один,
                # поэтому фактический ключ — name.
                obj, is_new = LegalEntity.objects.update_or_create(
                    name=name, inn=FNS_PARENT_INN,
                    defaults=defaults,
                )
                if is_new:
                    created += 1
                else:
                    updated += 1

        # ─── 2. Подведомственные организации (самостоятельные юр. лица) ─────
        if source in ("lower", "all"):
            rows = self.fetch_csv(LOWER_CSV_URL)
            data_rows = rows[1:]
            if limit:
                data_rows = data_rows[:limit]

            for row in data_rows:
                if len(row) < 11:
                    continue
                # поля: GA, GB, G1, G2, G3, G4, G5, G6, G7, G8, G9, G10
                full_name = (row[0] or "").strip()
                short_name = (row[1] or "").strip()
                address = (row[2] or "").strip()
                # row[3] — ФИО руководителя (пропускаем)
                phone = (row[4] or "").strip()
                # row[5] — описание ОКВЭД
                okpo = (row[6] or "").strip()
                ogrn = (row[7] or "").strip()
                inn = (row[8] or "").strip()
                email = (row[9] or "").strip() if "nan" not in row[9].lower() else ""
                website = (row[10] or "").strip() if len(row) > 10 and "nan" not in (row[10] or "").lower() else ""

                if not full_name or not inn:
                    continue

                defaults = {
                    "name": full_name,
                    "short_name": short_name[:255],
                    "entity_type": "other",
                    "kind": kind,
                    "status": "active",
                    "is_active": True,
                    "inn": inn[:12],
                    "ogrn": ogrn[:15],
                    "okpo": okpo[:14],
                    "legal_address": address,
                    "phone": phone[:20],
                    "email": email if "@" in email else "",
                    "website": website if website.startswith("http") or "." in website else "",
                    "notes": "Импорт из open data ФНС (lowerorganization)",
                    "director_name": "",
                    "director_title": "",
                }
                # website должен быть валидным URL; если нет схемы — префикс
                if defaults["website"] and not defaults["website"].startswith("http"):
                    defaults["website"] = "https://" + defaults["website"]

                self.stdout.write(f"  [ФКУ/ФБУ] {short_name or full_name[:60]} | ИНН={inn}")

                if dry_run:
                    continue

                obj, is_new = LegalEntity.objects.update_or_create(
                    inn=inn,
                    defaults=defaults,
                )
                if is_new:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано: {created}, обновлено: {updated}"
        ))
