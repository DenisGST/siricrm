"""Обновить снапшоты справочников ЕФРСБ из живого read-API (dev-контур).

Пишет apps/efrsb/reference_data/{message_types,court_decision_types}.json.
Запускать ТОЛЬКО на dev (как другие парсеры внешних API), затем коммитить JSON
и переносить на прод через git — на проде сеть к ЕФРСБ не дёргаем.

    python manage.py efrsb_pull_reference --contour demo --login demowebuser --password 'Ax!761BN'

Без флагов берёт креды/контур из settings (EFRSB_*).
"""
from __future__ import annotations

import json
import os

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

_REF_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "reference_data")


class Command(BaseCommand):
    help = "Обновить снапшоты справочников ЕФРСБ (message-types, court-decision-types) из read-API."

    def add_arguments(self, parser):
        parser.add_argument("--contour", choices=["demo", "prod"], default=None)
        parser.add_argument("--login", default=None)
        parser.add_argument("--password", default=None)

    def handle(self, *args, **opts):
        # Временно переопределяем настройки в процессе команды (dev-утилита).
        if opts["contour"]:
            settings.EFRSB_CONTOUR = opts["contour"]
        if opts["login"]:
            settings.EFRSB_LOGIN = opts["login"]
        if opts["password"]:
            settings.EFRSB_PASSWORD = opts["password"]
        settings.EFRSB_ENABLED = True
        cache.delete("efrsb:jwt")

        from apps.efrsb import client, config
        if not config.is_configured():
            raise CommandError("ЕФРСБ не настроен: передайте --login/--password или задайте EFRSB_* в env.")

        os.makedirs(_REF_DIR, exist_ok=True)
        pairs = [
            ("/v1/reference-books/message-types", "message_types.json"),
            ("/v1/reference-books/court-decision-types", "court_decision_types.json"),
        ]
        for path, fname in pairs:
            items, offset = [], 0
            while True:
                data = client._request("GET", path, params={"limit": 1000, "offset": offset}).json()
                page = (data.get("pageData") if isinstance(data, dict) else data) or []
                items += page
                total = data.get("total") if isinstance(data, dict) else len(page)
                offset += 1000
                if not page or (total and offset >= total):
                    break
            items.sort(key=lambda x: x.get("code", ""))
            with open(os.path.join(_REF_DIR, fname), "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            self.stdout.write(self.style.SUCCESS(
                f"• {fname}: {len(items)} записей "
                f"(актуальных {sum(1 for x in items if not x.get('isOld'))})."))
        self.stdout.write(self.style.SUCCESS(
            "Готово. Закоммитьте JSON и прогоните efrsb_seed."))
