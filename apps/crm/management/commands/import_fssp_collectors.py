"""
Импорт коллекторских агентств из официального реестра ФССП.

Источник — Государственный реестр юридических лиц, осуществляющих деятельность
по возврату просроченной задолженности в качестве основного вида деятельности.
XLS-выгрузка доступна на old.fssp.gov.ru (актуальна на 21.06.2023, 534 записи).

Реквизиты из XLS: наименование, ОГРН, ИНН, страховщик, адрес, сайт, рег.номер.
DaData используется по ИНН для получения чистого наименования, ОПФ, ОКПО и
формального юридического адреса (адрес из XLS часто с переносами).
Руководитель НЕ импортируется.
"""
import os
import re
import tempfile

import requests
import xlrd
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind
from apps.crm.management.commands.import_cbr_banks import (
    enrich_from_dadata,
    map_entity_type,
)
from apps.crm.management.commands.import_myfin_mfo import dadata_find_by_inn


FSSP_XLS_URL = (
    "https://old.fssp.gov.ru/files/fssp/db/files/"
    "reestr_yuridicheskih_lic_20230621_20236211130.xls"
)

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}

# В документе ФССП ИНН/ОГРН в одном столбце с переносом строки:
#   «ОГРН 1096315004720,\nИНН 6315626402»
INN_RE = re.compile(r"ИНН\s*(\d{10,12})")
OGRN_RE = re.compile(r"ОГРН\s*(\d{13,15})")


class Command(BaseCommand):
    help = "Импорт коллекторов из реестра ФССП (XLS) + обогащение DaData"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-enrich", action="store_true",
                            help="Не вызывать DaData, использовать только XLS")

    def handle(self, *args, **opts):
        limit = opts["limit"]
        dry_run = opts["dry_run"]
        no_enrich = opts["no_enrich"]

        kind = LegalEntityKind.objects.filter(short_name="КА").first()
        if not kind and not dry_run:
            self.stdout.write(self.style.ERROR(
                "В справочнике нет КА — проверь миграцию 0029."
            ))
            return

        # 1) Скачиваем XLS во временный файл
        self.stdout.write(f"Загружаю {FSSP_XLS_URL} …")
        r = requests.get(FSSP_XLS_URL, headers=HEADERS, timeout=60)
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as tmp:
            tmp.write(r.content)
            xls_path = tmp.name
        self.stdout.write(f"  размер: {len(r.content)} байт, → {xls_path}")

        try:
            wb = xlrd.open_workbook(xls_path)
            sh = wb.sheet_by_index(0)

            # 2) Собираем строки: первые ~8 строк — заголовок/легенда,
            # реальные данные — там, где в колонке 2 стоит целое число.
            entries = []
            for i in range(sh.nrows):
                try:
                    num = float(sh.cell_value(i, 2))
                    if num <= 0:
                        continue
                except (ValueError, TypeError):
                    continue
                # пропускаем легенду цветовой индикатор — если col7 пустая
                name = str(sh.cell_value(i, 7) or "").strip()
                if not name:
                    continue
                entries.append({
                    "num": int(num),
                    "risk_color": str(sh.cell_value(i, 3) or "").strip(),
                    "reg_number": str(sh.cell_value(i, 4) or "").strip(),
                    "reg_extra": str(sh.cell_value(i, 5) or "").strip(),
                    "reg_date": str(sh.cell_value(i, 6) or "").strip(),
                    "name": name,
                    "codes": str(sh.cell_value(i, 8) or "").strip(),
                    "insurance": str(sh.cell_value(i, 9) or "").strip(),
                    "address": str(sh.cell_value(i, 10) or "").strip(),
                    "website": str(sh.cell_value(i, 11) or "").strip(),
                })
            self.stdout.write(f"  записей в XLS: {len(entries)}")
            if limit:
                entries = entries[:limit]
        finally:
            try:
                os.unlink(xls_path)
            except OSError:
                pass

        # 3) Обрабатываем записи
        created = updated = skipped = 0
        for e in entries:
            inn_m = INN_RE.search(e["codes"])
            ogrn_m = OGRN_RE.search(e["codes"])
            inn = inn_m.group(1) if inn_m else ""
            ogrn = ogrn_m.group(1) if ogrn_m else ""

            if not inn:
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f"  [{e['num']}] ПРОПУСК (нет ИНН): {e['name'][:60]}"
                ))
                continue

            # Обогащение по ИНН
            enrichment = {}
            dd = None
            if not no_enrich:
                dd = dadata_find_by_inn(inn)
                enrichment = enrich_from_dadata(dd) if dd else {}

            # адрес из XLS — с переносами, приберёмся:
            raw_addr = re.sub(r"\s+", " ", e["address"]).strip()

            defaults = {
                "name": enrichment.get("name") or e["name"],
                "short_name": (enrichment.get("short_name") or e["name"])[:255],
                "kind": kind,
                "entity_type": "other",
                "status": "active",
                "is_active": True,
                "inn": inn[:12],
                "ogrn": (enrichment.get("ogrn") or ogrn)[:15] if (enrichment.get("ogrn") or ogrn) else ogrn[:15],
                "kpp": enrichment.get("kpp", ""),
                "okpo": enrichment.get("okpo", ""),
                "okved": enrichment.get("okved", ""),
                "legal_address": enrichment.get("legal_address") or raw_addr,
                "phone": enrichment.get("phone", ""),
                "email": enrichment.get("email", ""),
                "website": (
                    e["website"] if e["website"].startswith("http")
                    else (f"https://{e['website']}" if e["website"] and "." in e["website"] else "")
                ),
                "director_name": "",
                "director_title": "",
                "notes": (
                    f"Импорт из реестра коллекторов ФССП\n"
                    f"№ в реестре: {e['num']}\n"
                    f"Рег.номер: {e['reg_number']}\n"
                    f"Дата включения: {e['reg_date']}\n"
                    f"Риск-категория (цвет): {e['risk_color'] or '—'}\n"
                    f"Страховщик:\n{e['insurance']}"
                ),
            }

            # OПФ из DaData
            opf = ((dd or {}).get("opf") or {}).get("short") if dd else ""
            if opf:
                defaults["entity_type"] = map_entity_type(opf)

            self.stdout.write(
                f"  [{e['num']}] {defaults['name'][:60]} | ИНН={inn}"
                f"{' (DaData)' if dd else ''}"
            )

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
            f"Готово. Создано: {created}, обновлено: {updated}, пропущено: {skipped}"
        ))
