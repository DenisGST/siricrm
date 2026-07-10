"""Импорт органов гостехнадзора (надзор за самоходными машинами) в LegalEntity.

Один орган на регион, но названия в ЕГРЮЛ сильно разнятся (Инспекция
гостехнадзора / Гостехнадзор / Управление по надзору за самоходными машинами /
часть комитета сельского хозяйства). Поэтому по региону пробуем несколько
формулировок с фильтром locations kladr и берём первое совпадение по региону.
Покрытие частичное — регионы, где орган назван иначе, останутся на ручном вводе.
Идемпотентно по (kind=Гостехнадзор, region). Привязывает req_gostehnadzor к виду.

🛑 DaData-квота — парсить ТОЛЬКО на dev, затем dev→prod (external-api-dev-then-sync).
"""
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind, Region

_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
_QUERIES = [
    "гостехнадзор",
    "инспекция гостехнадзора",
    "государственная инспекция по надзору за техническим состоянием самоходных машин",
    "надзору за техническим состоянием самоходных машин",
]
_MARKERS = ("ГОСТЕХНАДЗОР", "САМОХОДН", "ТЕХНИЧЕСКОГО СОСТОЯНИЯ", "ТЕХНИЧЕСКИМ СОСТОЯНИЕМ")


def _headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {getattr(settings, 'DADATA_API_KEY', '')}",
    }


def _try(query, kladr, reg_number):
    body = {"query": query, "count": 10, "locations": [{"kladr_id": kladr}]}
    for _ in range(3):
        try:
            r = requests.post(_URL, json=body, headers=_headers(), timeout=15)
            if r.status_code == 429:
                time.sleep(1.5)
                continue
            r.raise_for_status()
            sugg = r.json().get("suggestions") or []
            break
        except Exception:
            time.sleep(1.0)
            sugg = []
    for s in sugg:
        d = s.get("data") or {}
        addr = d.get("address") or {}
        k2 = ((addr.get("data") or {}).get("region_kladr_id", "") or "")[:2]
        nm = ((d.get("name") or {}).get("short_with_opf") or "").upper()
        if k2 and k2.isdigit() and int(k2) == reg_number and any(m in nm for m in _MARKERS):
            return {
                "name": (d.get("name") or {}).get("full_with_opf") or s.get("value") or "",
                "short_name": (d.get("name") or {}).get("short_with_opf") or "",
                "inn": d.get("inn") or "",
                "kpp": d.get("kpp") or "",
                "ogrn": d.get("ogrn") or "",
                "legal_address": addr.get("unrestricted_value") or addr.get("value") or "",
            }
    return None


def _dadata_gostehnadzor(reg: Region):
    kladr = f"{reg.number:02d}00000000000"
    for q in _QUERIES:
        data = _try(q, kladr, reg.number)
        time.sleep(0.05)
        if data:
            return data
    return None


class Command(BaseCommand):
    help = "Импорт органов гостехнадзора по регионам из DaData (частичное покрытие)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--regions", default="",
                            help="номера регионов через запятую (для теста)")

    def handle(self, *args, **opts):
        if not getattr(settings, "DADATA_API_KEY", ""):
            self.stderr.write("DADATA_API_KEY не задан")
            return
        kind, _ = LegalEntityKind.objects.get_or_create(
            short_name="Гостехнадзор",
            defaults={"name": "Орган государственного надзора за техническим "
                              "состоянием самоходных машин (гостехнадзор)"},
        )
        regions = Region.objects.all().order_by("number")
        if opts["regions"]:
            nums = [int(x) for x in opts["regions"].split(",") if x.strip().isdigit()]
            regions = regions.filter(number__in=nums)

        created = updated = missed = 0
        misses = []
        for reg in regions:
            data = _dadata_gostehnadzor(reg)
            if not data:
                missed += 1
                misses.append(reg.number)
                continue
            self.stdout.write(f"  [{reg.number:>3}] {data['short_name'][:52]}")
            if opts["dry_run"]:
                continue
            defaults = {
                **data, "kind": kind, "region": reg,
                "entity_type": "other", "status": "active", "is_active": True,
            }
            obj = LegalEntity.objects.filter(kind=kind, region=reg).first()
            if obj:
                for k, v in defaults.items():
                    setattr(obj, k, v)
                obj.save()
                updated += 1
            else:
                LegalEntity.objects.create(**defaults)
                created += 1

        if not opts["dry_run"]:
            from apps.procedure.models import RequestType
            RequestType.objects.filter(code="req_gostehnadzor").update(recipient_kind=kind)
        self.stdout.write(self.style.SUCCESS(
            f"Гостехнадзор: создано {created}, обновлено {updated}, не найдено {missed}"))
        if misses:
            self.stdout.write(f"  регионы без органа (ручной ввод): {misses}")
