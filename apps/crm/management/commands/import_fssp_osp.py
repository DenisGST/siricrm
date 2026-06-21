"""Импорт реестра отделов судебных приставов (ОСП) ФССП в crm.LegalEntity.

Источник — открытые данные ФССП:
  https://opendata.fssp.gov.ru/7709576929-osp

CSV содержит поля (header на английском): код субъекта, код терр.органа,
наименование, почтовый адрес, ФИО начальника, телефон, факс, режим работы,
территория обслуживания, координаты, URL. ~2700 строк, обновляется ежемесячно.

Адрес нормализуем через DaData /clean/address (нужны и API_KEY, и SECRET_KEY).
Идемпотентность — по LegalEntity.fssp_code (код терр.органа, напр. "34005").

  python manage.py import_fssp_osp --limit 100   # тестовый прогон
  python manage.py import_fssp_osp --dry-run
  python manage.py import_fssp_osp               # полный
"""
import csv
import io
import os
import time
from collections import Counter

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.crm.models import LegalEntity, LegalEntityKind, Region


META_URL = "https://opendata.fssp.gov.ru/opendata/7709576929-osp/meta.csv"
BASE_URL = "https://opendata.fssp.gov.ru/7709576929-osp/"
CLEAN_ADDRESS_URL = "https://cleaner.dadata.ru/api/v1/clean/address"


def _latest_data_url():
    """Из meta.csv достаём САМУЮ СВЕЖУЮ ссылку data-{date}-structure-{date}.csv.

    Формат meta.csv (через запятую, стандартный CSV):
      property,value
      data-{YYYYMMDDTHHmm}-structure-{...},https://opendata.fssp.gov.ru/7709576929-osp/...
      ...

    Версий сотни — берём последнюю по дате префикса (YYYYMMDD).
    """
    r = requests.get(META_URL, timeout=30)
    r.raise_for_status()
    text = r.content.decode("utf-8", errors="ignore")
    candidates = []  # (date_prefix, url)
    for line in csv.reader(io.StringIO(text)):
        if not line or len(line) < 2:
            continue
        key = line[0].strip()
        val = line[1].strip()
        if not key.startswith("data-"):
            continue
        # ключ: data-20260526T0000-structure-20251205T1400
        date_part = key[len("data-"):].split("-", 1)[0]  # 20260526T0000
        candidates.append((date_part, val))
    if not candidates:
        raise RuntimeError("В meta.csv не найдено ни одной ссылки data-…")
    # Сортируем лексикографически — формат YYYYMMDDTHHMM сравнивается корректно.
    candidates.sort()
    return candidates[-1][1]


def _dadata_clean_address(address, api_key, secret_key, timeout=15):
    """DaData cleaner /clean/address. Возвращает dict с ключом `result`
    (нормализованный адрес) и компонентами, либо None при ошибке.
    """
    if not address:
        return None
    try:
        r = requests.post(
            CLEAN_ADDRESS_URL,
            json=[address],
            headers={
                "Authorization": f"Token {api_key}",
                "X-Secret": secret_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        r.raise_for_status()
        result = r.json()
        return result[0] if result else None
    except Exception:
        return None


def _pick(row, *candidates):
    """Берёт значение из CSV-строки по первому ключу из candidates что нашёлся."""
    for c in candidates:
        if c in row and row[c] is not None:
            return (row[c] or "").strip()
    return ""


class Command(BaseCommand):
    help = "Импорт ОСП ФССП из opendata.fssp.gov.ru → crm.LegalEntity (kind=ФССП)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                            help="Ограничить число обрабатываемых строк (для теста).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Не сохранять — только отчёт.")
        parser.add_argument("--no-dadata", action="store_true",
                            help="Не нормализовывать адрес через DaData "
                                 "(использовать как есть из CSV).")

    def handle(self, *args, **opts):
        limit = opts["limit"]
        dry = opts["dry_run"]
        use_dadata = not opts["no_dadata"]

        api_key = getattr(settings, "DADATA_API_KEY", "") or os.environ.get("DADATA_API_KEY", "")
        secret_key = getattr(settings, "DADATA_SECRET_KEY", "") or os.environ.get("DADATA_SECRET_KEY", "")
        if use_dadata and (not api_key or not secret_key):
            self.stderr.write("DADATA_API_KEY/DADATA_SECRET_KEY не заданы. "
                              "Использую --no-dadata если хочешь продолжить без нормализации.")
            return

        # Тип «ФССП»
        kind_fssp = LegalEntityKind.objects.filter(short_name__iexact="ФССП").first()
        if kind_fssp is None:
            kind_fssp = LegalEntityKind.objects.filter(name__icontains="ФССП").first()
        if kind_fssp is None:
            self.stderr.write("LegalEntityKind «ФССП» не найден")
            return
        self.stdout.write(f"Используем kind: {kind_fssp}")

        # Скачиваем CSV
        self.stdout.write("Получаем актуальный CSV…")
        data_url = _latest_data_url()
        self.stdout.write(f"  URL: {data_url}")
        r = requests.get(data_url, timeout=180)
        r.raise_for_status()
        # Кодировка может быть UTF-8 (с BOM) или windows-1251 — пробуем обе.
        raw = r.content
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", errors="ignore")
        self.stdout.write(f"  Получено {len(raw)} байт, {len(text.splitlines())} строк")

        reader = csv.DictReader(io.StringIO(text))  # csv по умолчанию: разделитель ','
        # Логируем заголовки чтобы убедиться что не сломаны
        self.stdout.write(f"  Заголовки: {reader.fieldnames}")

        # Регионы → словарь {number: Region}
        regions_by_number = {rg.number: rg for rg in Region.objects.all()}

        stats = Counter()

        for i, row in enumerate(reader):
            if limit and i >= limit:
                break

            # Поля CSV ФССП (английские названия из структуры 2024 г.)
            code = _pick(row, "code of the territorial agency",
                              "Код территориального органа")
            name = _pick(row, "name of the territorial agency",
                              "Наименование территориального органа")
            postal = _pick(row, "postal address", "Почтовый адрес")
            phone = _pick(row, "telephone number", "Телефон")
            url_field = _pick(row, "URL", "URL территориального органа")
            region_code = _pick(row, "region code", "Код субъекта Российской Федерации")
            working_hours = _pick(row, "working hours of the territorial agency",
                                       "Режим работы")

            if not code or not name:
                stats["skipped_no_data"] += 1
                continue

            # ⚠ `code of the territorial agency` уникален только В ПРЕДЕЛАХ
            # региона (у каждого региона свой «ОСП №1»). Составляем
            # глобально-уникальный ключ region-code.
            fssp_key = f"{region_code or '0'}-{code}"

            # Регион по region_code из CSV
            region = None
            try:
                region = regions_by_number.get(int(region_code))
            except (ValueError, TypeError):
                region = None

            # Нормализация адреса DaData
            normalized = postal
            cleaned = None
            if use_dadata and postal:
                cleaned = _dadata_clean_address(postal, api_key, secret_key)
                if cleaned and cleaned.get("result"):
                    normalized = cleaned["result"]
                    stats["dadata_clean_ok"] += 1
                else:
                    stats["dadata_clean_fail"] += 1
                # DaData free tier — 30 req/sec; маленькая пауза.
                time.sleep(0.05)

            # Фолбэк региона: если в CSV region_code не маппится (СОСП/ГМУ
            # с кодом 98), берём из нормализованного DaData ответа.
            # region_kladr_id формата "7700000000000" — первые 2 цифры
            # соответствуют Region.number.
            if region is None and cleaned:
                kladr = cleaned.get("region_kladr_id") or ""
                if kladr and len(kladr) >= 2:
                    try:
                        region = regions_by_number.get(int(kladr[:2]))
                    except (ValueError, TypeError):
                        region = None
                if region is None and cleaned.get("region"):
                    rname = (cleaned.get("region") or "").strip()
                    if rname:
                        region = (Region.objects.filter(name__iexact=rname).first()
                                  or Region.objects.filter(name__icontains=rname).first())
                if region is not None:
                    stats["region_from_dadata"] += 1
            if region is None:
                stats["no_region"] += 1

            if dry:
                stats["would_create_or_update"] += 1
                continue

            # Notes — телефон не помещается; режим работы кладём в notes.
            notes_parts = []
            if working_hours:
                notes_parts.append(f"Режим работы: {working_hours}")
            notes = "\n".join(notes_parts)

            defaults = {
                "name": name[:500],
                "short_name": name[:255],
                "entity_type": "other",
                "kind": kind_fssp,
                "region": region,
                "postal_address": normalized,
                "legal_address": normalized,
                "actual_address": normalized,
                "phone": phone[:20],
                "website": url_field[:200],
                "notes": notes,
                "is_active": True,
            }
            try:
                _, created = LegalEntity.objects.update_or_create(
                    fssp_code=fssp_key, defaults=defaults,
                )
                stats["created" if created else "updated"] += 1
            except Exception as exc:
                stats["error"] += 1
                if stats["error"] <= 3:
                    self.stderr.write(f"  ! err on {fssp_key} {name[:40]!r}: {exc}")

            if (i + 1) % 100 == 0:
                self.stdout.write(
                    f"  …обработано {i+1}: "
                    f"created={stats['created']} updated={stats['updated']} "
                    f"dadata_ok={stats['dadata_clean_ok']} fail={stats['dadata_clean_fail']}"
                )

        # Отчёт
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== ИТОГ ==="))
        for k in ("created", "updated", "would_create_or_update",
                  "skipped_no_data", "no_region", "region_from_dadata",
                  "dadata_clean_ok", "dadata_clean_fail", "error"):
            self.stdout.write(f"  {k}: {stats[k]}")
