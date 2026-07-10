"""Добить почтовый индекс госорганам, у которых в адресе индекса нет.

Нужно для конвертов Почты РФ: у части видов (особенно МРЭО ГИБДД) адрес без
индекса → на конверте пустое поле «Индекс». Берём индекс через DaData
clean/address и кладём в LegalEntity.postal_code. Идемпотентно (пропускаем тех,
у кого индекс уже есть в postal_code или извлекается из адреса).

🛑 DaData — квота 10k/день на ключ. Гонять ТОЛЬКО на dev, затем dev→prod
(postal_code переносится в составе LegalEntity; см. external-api-dev-then-sync).
"""
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind

CLEAN_URL = "https://cleaner.dadata.ru/api/v1/clean/address"
_IDX_RE = re.compile(r"\b(\d{6})\b")


def _has_index(le) -> bool:
    if (le.postal_code or "").strip():
        return True
    for a in (le.postal_address, le.legal_address, le.actual_address):
        if a and _IDX_RE.search(a):
            return True
    return False


def _clean(address, api_key, secret):
    try:
        r = requests.post(
            CLEAN_URL, json=[address],
            headers={"Authorization": f"Token {api_key}", "X-Secret": secret,
                     "Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 429:
            time.sleep(1.5)
            return None
        r.raise_for_status()
        res = r.json()
        return res[0] if res else None
    except Exception:
        return None


class Command(BaseCommand):
    help = "Добить postal_code госорганам без индекса через DaData clean/address"

    def add_arguments(self, parser):
        parser.add_argument("--kinds", default="МРЭО",
                            help="short_name видов через запятую (по умолч. МРЭО)")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        api_key = getattr(settings, "DADATA_API_KEY", "")
        secret = getattr(settings, "DADATA_SECRET_KEY", "")
        if not (api_key and secret):
            self.stderr.write("Нужны DADATA_API_KEY и DADATA_SECRET_KEY (cleaner)")
            return
        kind_names = [s.strip() for s in opts["kinds"].split(",") if s.strip()]
        kinds = list(LegalEntityKind.objects.filter(short_name__in=kind_names))
        if not kinds:
            self.stderr.write(f"Виды не найдены: {kind_names}")
            return
        qs = LegalEntity.objects.filter(kind__in=kinds).order_by("name")

        todo = [le for le in qs if not _has_index(le)]
        if opts["limit"]:
            todo = todo[: opts["limit"]]
        self.stdout.write(f"Без индекса: {len(todo)} из {qs.count()}")

        done = miss = 0
        for le in todo:
            addr = (le.postal_address or le.legal_address or le.actual_address or "").strip()
            if not addr:
                miss += 1
                continue
            cleaned = _clean(addr, api_key, secret)
            time.sleep(0.05)
            idx = (cleaned or {}).get("postal_code") if cleaned else None
            if not idx:
                miss += 1
                continue
            self.stdout.write(f"  {idx}  {le.short_name[:55]}")
            if opts["dry_run"]:
                continue
            le.postal_code = idx[:6]
            le.save(update_fields=["postal_code"])
            done += 1
        self.stdout.write(self.style.SUCCESS(
            f"Готово: заполнено {done}, без индекса {miss}"))
