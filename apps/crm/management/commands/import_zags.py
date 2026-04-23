"""
Импорт органов ЗАГС из официального справочника Минфина.

Источник: https://minfin.gov.ru/common/upload/library/2018/01/main/
          spravochnik_organov_ZAGS_1.xlsx
Лист SOZAGS_1, 6723 записи (все виды: региональные управления, отделы, МФЦ,
консульства, диппредставительства).

Колонки:
  0 — Код органа ЗАГС (R0100000 и т.д.)
  1 — Код вида органа (01-05, 11, 12)
  2 — Код правоприемника
  3 — Код вышестоящего органа
  4 — Код органа хранения бумажных записей
  5 — Наименование органа ЗАГС
  6 — Адрес

У органов ЗАГС нет индивидуальных ИНН (это структурные подразделения).
Руководителя не импортируем.
"""
import os
import re
import tempfile

import openpyxl
import requests
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind


XLSX_URL = (
    "https://minfin.gov.ru/common/upload/library/2018/01/main/"
    "spravochnik_organov_ZAGS_1.xlsx"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}


VID_NAMES = {
    "01": "орган ЗАГС исполнительной власти субъекта РФ",
    "02": "орган ЗАГС в структуре регионального управления",
    "03": "орган ЗАГС в структуре местного самоуправления",
    "04": "орган МСУ сельского поселения",
    "05": "многофункциональный центр",
    "11": "консульское учреждение",
    "12": "дипломатическое представительство",
}


class Command(BaseCommand):
    help = "Импорт реестра органов ЗАГС из справочника Минфина"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        limit = opts["limit"]
        dry_run = opts["dry_run"]

        kind = LegalEntityKind.objects.filter(short_name="ЗАГС").first()
        if not kind and not dry_run:
            self.stdout.write(self.style.ERROR(
                "Нет LegalEntityKind(ЗАГС). Прогоните миграцию 0032."
            ))
            return

        # 1) Скачиваем xlsx
        self.stdout.write(f"Загружаю {XLSX_URL} …")
        r = requests.get(XLSX_URL, headers=HEADERS, timeout=60)
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(r.content)
            xlsx_path = tmp.name
        self.stdout.write(f"  размер: {len(r.content)} байт")

        try:
            wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
            sh = wb["SOZAGS_1"]

            entries = []
            for row in sh.iter_rows(min_row=4, values_only=True):
                code = row[0]
                if not code or not str(code).startswith("R"):
                    continue
                vid = str(row[1] or "").zfill(2)
                parent_code = str(row[3] or "").strip()
                name = (row[5] or "").strip() if row[5] else ""
                address = (row[6] or "").strip() if row[6] else ""
                # нормализуем адрес: убираем переносы строк и табы
                address = re.sub(r"\s+", " ", address).strip(" ,")
                if not name:
                    continue
                entries.append({
                    "code": str(code).strip(),
                    "vid": vid,
                    "parent": parent_code,
                    "name": name,
                    "address": address,
                })
            self.stdout.write(f"  записей в xlsx: {len(entries)}")
            if limit:
                entries = entries[:limit]
        finally:
            try:
                os.unlink(xlsx_path)
            except OSError:
                pass

        created = updated = 0
        for e in entries:
            vid_label = VID_NAMES.get(e["vid"], f"вид {e['vid']}")
            notes = (
                f"Импорт из справочника Минфина (spravochnik_organov_ZAGS_1.xlsx)\n"
                f"Код органа ЗАГС: {e['code']}\n"
                f"Вид: {e['vid']} — {vid_label}\n"
                f"Вышестоящий орган: {e['parent'] or '—'}"
            )

            defaults = {
                "name": e["name"],
                "short_name": e["name"][:255],
                "kind": kind,
                "entity_type": "other",
                "status": "active",
                "is_active": True,
                "okpo": e["code"][:14],      # код ЗАГС сохраняем как ОКПО-заменитель
                "legal_address": e["address"],
                "director_name": "",
                "director_title": "",
                "notes": notes,
            }

            if dry_run:
                self.stdout.write(
                    f"  {e['code']:8} [{e['vid']}] {e['name'][:70]}"
                )
                continue

            # Уникальность — по коду органа ЗАГС (он уникален в реестре).
            obj, is_new = LegalEntity.objects.update_or_create(
                okpo=e["code"][:14], kind=kind,
                defaults=defaults,
            )
            if is_new:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано: {created}, обновлено: {updated}"
        ))
