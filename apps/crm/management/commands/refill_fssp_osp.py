"""Добивает ОСП ФССП по двум критериям:
  1. Сырой postal_address (содержит «, ,» — признак ненормализованного CSV) →
     повторно прогоняем через DaData /clean/address.
  2. Регион не проставлен → пытаемся извлечь из:
     а) DaData clean (если адрес есть),
     б) суффикса имени «… по Москве / по Краснодарскому краю / по Херсонской
        области» — сопоставляем нормализованный фрагмент с Region.name.

Идемпотентна: можно гонять многократно, обновляются только подходящие записи.

  python manage.py refill_fssp_osp                # все подходящие
  python manage.py refill_fssp_osp --only-region  # только регион (без обновления адреса)
  python manage.py refill_fssp_osp --only-address # только адрес
"""
import os
import re
import time
from collections import Counter

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, Region


CLEAN_ADDRESS_URL = "https://cleaner.dadata.ru/api/v1/clean/address"


def _dadata_clean(addr, api_key, secret_key):
    if not addr:
        return None
    try:
        r = requests.post(
            CLEAN_ADDRESS_URL, json=[addr],
            headers={
                "Authorization": f"Token {api_key}",
                "X-Secret": secret_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        return (r.json() or [None])[0]
    except Exception:
        return None


def _is_raw_address(addr: str) -> bool:
    """Признак сырого CSV-адреса: подряд идущие запятые с пробелами."""
    if not addr:
        return False
    return bool(re.search(r",\s*,", addr))


# Нормализация фрагмента-региона (склонения → именительный)
_SUFFIX_RULES = [
    (re.compile(r"скому краю$", re.IGNORECASE), "ский край"),
    (re.compile(r"скому округу$", re.IGNORECASE), "ский округ"),
    (re.compile(r"ской области$", re.IGNORECASE), "ская область"),
    (re.compile(r"ской обл\.?$", re.IGNORECASE), "ская область"),
    (re.compile(r"скому АО$", re.IGNORECASE), "ский АО"),
    (re.compile(r"ской республике$", re.IGNORECASE), "ская республика"),
    (re.compile(r"Москве$", re.IGNORECASE), "Москва"),
    (re.compile(r"Санкт-Петербургу$", re.IGNORECASE), "Санкт-Петербург"),
    (re.compile(r"Севастополю$", re.IGNORECASE), "Севастополь"),
]


def _denormalize(fragment: str) -> str:
    """Превращает «Москве»→«Москва», «Краснодарскому краю»→«Краснодарский край»."""
    s = fragment.strip()
    for pat, repl in _SUFFIX_RULES:
        new = pat.sub(repl, s)
        if new != s:
            return new
    return s


def _extract_region_from_name(name, regions_by_norm_name):
    """Из имени «СО по ОЗ ФССП России по Москве» вытаскиваем «Москва» и ищем
    в словаре нормализованных имён Region. Берём ПОСЛЕДНЕЕ совпадение «по …»
    — в имени может быть несколько «по» (по ОЗ ФССП России по Москве)."""
    if not name:
        return None
    matches = re.findall(r"\bпо\s+(.+?)(?:$|(?=\s+по\s))", name)
    if not matches:
        return None
    # Берём последнее — обычно это и есть регион
    fragment = _denormalize(matches[-1])
    norm = fragment.lower().strip()
    norm = re.sub(r"^г[\.\s]+", "", norm).strip()
    return regions_by_norm_name.get(norm)


def _region_from_fssp_code(fssp_code, regions_by_number):
    """Из составного ключа «{region_number}-{code}» берём регион по префиксу.

    Для большинства ОСП это совпадает с настоящим регионом. ⚠ для спец-кода
    98 (ГМУ ФССП) — НЕ работает, эти ОСП обслуживают много регионов и должны
    определяться через имя/адрес."""
    if not fssp_code or "-" not in fssp_code:
        return None
    head = fssp_code.split("-", 1)[0]
    try:
        n = int(head)
    except (ValueError, TypeError):
        return None
    if n == 98:  # ГМУ — спец-префикс, не регион
        return None
    return regions_by_number.get(n)


def _build_regions_index():
    """Словарь {нормализованное_имя: Region}.

    Содержит несколько форм одной записи: «Краснодарский край», «краснодарский край»,
    «москва» / «г москва» и т.п. Для устойчивого матчинга.
    """
    idx = {}
    for r in Region.objects.all():
        names = {r.name}
        # Без префикса «г. »
        stripped = re.sub(r"^г[\.\s]+", "", r.name, flags=re.IGNORECASE).strip()
        names.add(stripped)
        for n in names:
            idx[n.lower()] = r
    return idx


class Command(BaseCommand):
    help = "Добивка ОСП ФССП: нормализация сырых адресов + проставление региона из имени."

    def add_arguments(self, parser):
        parser.add_argument("--only-region", action="store_true",
                            help="Только заполнить регион (не трогать адрес).")
        parser.add_argument("--only-address", action="store_true",
                            help="Только нормализовать сырой адрес.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Ограничить число обрабатываемых записей.")

    def handle(self, *args, **opts):
        only_region = opts["only_region"]
        only_address = opts["only_address"]
        limit = opts["limit"]

        api_key = getattr(settings, "DADATA_API_KEY", "") or os.environ.get("DADATA_API_KEY", "")
        secret_key = getattr(settings, "DADATA_SECRET_KEY", "") or os.environ.get("DADATA_SECRET_KEY", "")
        if not api_key or not secret_key:
            self.stderr.write("DADATA_API_KEY/DADATA_SECRET_KEY не заданы")
            return

        regions_by_number = {r.number: r for r in Region.objects.all()}
        regions_by_name = _build_regions_index()
        self.stdout.write(
            f"Регионов: {len(regions_by_number)}, "
            f"уникальных нормализованных имён: {len(regions_by_name)}"
        )

        # Подбираем строки которые требуют добивки
        qs = LegalEntity.objects.filter(fssp_code__isnull=False).exclude(fssp_code="")
        total = qs.count()
        self.stdout.write(f"Всего ОСП в базе: {total}")

        stats = Counter()
        processed = 0
        for le in qs.iterator(chunk_size=500):
            if limit and processed >= limit:
                break

            need_addr = (not only_region) and _is_raw_address(le.postal_address)
            need_region = (not only_address) and (le.region_id is None)
            if not need_addr and not need_region:
                continue

            # Сначала пытаемся вытащить регион из имени (бесплатно, без DaData)
            if need_region:
                r = _extract_region_from_name(le.name, regions_by_name)
                if r is not None:
                    le.region = r
                    stats["region_from_name"] += 1
                    need_region = False  # уже взяли

            # Второй фолбэк: префикс fssp_code (если 84-x → Region 84 итд.)
            if need_region:
                r = _region_from_fssp_code(le.fssp_code, regions_by_number)
                if r is not None:
                    le.region = r
                    stats["region_from_fssp_code"] += 1
                    need_region = False

            # Если ещё нужен адрес или регион — идём в DaData
            cleaned = None
            if (need_addr or need_region) and le.postal_address:
                cleaned = _dadata_clean(le.postal_address, api_key, secret_key)
                if cleaned and cleaned.get("result"):
                    stats["dadata_ok"] += 1
                else:
                    stats["dadata_fail"] += 1
                time.sleep(0.05)

            # Обновляем адрес если сырой и DaData дала результат
            if need_addr and cleaned and cleaned.get("result"):
                normalized = cleaned["result"]
                le.legal_address = normalized
                le.actual_address = normalized
                le.postal_address = normalized
                stats["address_normalized"] += 1

            # Регион из DaData
            if need_region and cleaned:
                kladr = cleaned.get("region_kladr_id") or ""
                if kladr and len(kladr) >= 2:
                    try:
                        r = regions_by_number.get(int(kladr[:2]))
                        if r is not None:
                            le.region = r
                            stats["region_from_dadata"] += 1
                            need_region = False
                    except (ValueError, TypeError):
                        pass
                if need_region and cleaned.get("region"):
                    rname = (cleaned.get("region") or "").strip().lower()
                    r = regions_by_name.get(rname)
                    if r is not None:
                        le.region = r
                        stats["region_from_dadata"] += 1
                        need_region = False

            if need_region:
                stats["region_still_missing"] += 1

            try:
                le.save(update_fields=[
                    "region", "legal_address", "actual_address", "postal_address",
                ])
            except Exception as exc:
                stats["error"] += 1
                if stats["error"] <= 3:
                    self.stderr.write(f"  ! err on {le.fssp_code}: {exc}")

            processed += 1
            if processed % 100 == 0:
                self.stdout.write(
                    f"  …обработано {processed}: "
                    f"addr_norm={stats['address_normalized']} "
                    f"reg_name={stats['region_from_name']} "
                    f"reg_dadata={stats['region_from_dadata']} "
                    f"reg_miss={stats['region_still_missing']} "
                    f"dadata_fail={stats['dadata_fail']}"
                )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== ИТОГ ==="))
        for k in ("address_normalized", "region_from_name", "region_from_fssp_code",
                  "region_from_dadata", "region_still_missing",
                  "dadata_ok", "dadata_fail", "error"):
            self.stdout.write(f"  {k}: {stats[k]}")

        no_reg = LegalEntity.objects.filter(
            fssp_code__isnull=False, region__isnull=True,
        ).exclude(fssp_code="").count()
        self.stdout.write(f"\nИтоговое состояние БД: ОСП без региона = {no_reg}")
