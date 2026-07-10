"""Импорт отделений Социального фонда России (ОСФР) в LegalEntity.

Один ОСФР на регион (у каждого свой ИНН и региональный адрес). Источник —
DaData suggest/party с фильтром по региону (locations kladr). Идемпотентно по
(kind=СФР, region). Заодно привязывает тип запроса req_sfr к виду СФР.

🛑 DaData — квота 10k/день на ключ. Парсить ТОЛЬКО на dev, затем переносить
dev→prod через dumpdata/loaddata (см. память external-api-dev-then-sync).
"""
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind, Region

_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
_QUERY = "отделение фонда пенсионного и социального страхования"
_MARKERS = ("ОСФР", "СОЦИАЛЬН", "ПЕНСИ")


def _headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {getattr(settings, 'DADATA_API_KEY', '')}",
    }


def _dadata_osfr(reg: Region):
    """Вернуть dict реквизитов ОСФР региона или None."""
    kladr = f"{reg.number:02d}00000000000"
    body = {"query": _QUERY, "count": 8, "locations": [{"kladr_id": kladr}]}
    for attempt in range(3):
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
        if k2 and k2.isdigit() and int(k2) == reg.number and any(m in nm for m in _MARKERS):
            return {
                "name": (d.get("name") or {}).get("full_with_opf") or s.get("value") or "",
                "short_name": (d.get("name") or {}).get("short_with_opf") or "",
                "inn": d.get("inn") or "",
                "kpp": d.get("kpp") or "",
                "ogrn": d.get("ogrn") or "",
                "legal_address": addr.get("unrestricted_value") or addr.get("value") or "",
            }
    return None


class Command(BaseCommand):
    help = "Импорт ОСФР (отделения СФР) по регионам из DaData"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--regions", default="",
                            help="номера регионов через запятую (для теста)")

    def handle(self, *args, **opts):
        if not getattr(settings, "DADATA_API_KEY", ""):
            self.stderr.write("DADATA_API_KEY не задан")
            return
        kind, _ = LegalEntityKind.objects.get_or_create(
            short_name="СФР",
            defaults={"name": "Отделение Социального фонда России (СФР/ПФР)"},
        )
        regions = Region.objects.all().order_by("number")
        if opts["regions"]:
            nums = [int(x) for x in opts["regions"].split(",") if x.strip().isdigit()]
            regions = regions.filter(number__in=nums)

        created = updated = missed = 0
        for reg in regions:
            data = _dadata_osfr(reg)
            time.sleep(0.05)
            if not data:
                missed += 1
                self.stdout.write(f"  [{reg.number:>3}] {reg.name[:34]:34} — не найдено")
                continue
            self.stdout.write(f"  [{reg.number:>3}] {data['short_name'][:50]}")
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
            n = RequestType.objects.filter(code="req_sfr").update(recipient_kind=kind)
            self.stdout.write(f"  req_sfr → вид СФР: обновлено типов {n}")
        self.stdout.write(self.style.SUCCESS(
            f"ОСФР: создано {created}, обновлено {updated}, не найдено {missed}"))
