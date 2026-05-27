"""Команда для ручной отладки парсера kad.arbitr.ru.

Запускать ВНУТРИ контейнера arbitr-runner (там стоит playwright + Chromium):
    docker compose -f docker-compose.prod.yml --env-file .env.dev exec arbitr-runner \\
        python manage.py kad_probe open
    docker compose -f ... exec arbitr-runner \\
        python manage.py kad_probe search "Иванов И.И."
    docker compose -f ... exec arbitr-runner \\
        python manage.py kad_probe case https://kad.arbitr.ru/Card/<uuid>

Аргумент --headed запускает Chromium в графическом режиме — нужен X-сервер
внутри контейнера (для локальной отладки можно поставить xvfb-run).
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict

from django.core.management.base import BaseCommand, CommandError

from apps.arbitr.parsers.kad import (
    KadCaptchaRequired,
    KadParserError,
    KadSession,
)


class Command(BaseCommand):
    help = "Ручная отладка парсера kad (open/search/case)."

    def add_arguments(self, parser):
        parser.add_argument(
            "mode", choices=["open", "search", "case"],
            help=(
                "open  — открыть главную и проверить капчу; "
                "search FIO — поиск по ФИО; "
                "case URL — распарсить карточку дела"
            ),
        )
        parser.add_argument(
            "params", nargs="*",
            help="FIO (для search) или URL карточки (для case)",
        )
        parser.add_argument(
            "--court", default="",
            help="Код суда для фильтрации (например 'А12')",
        )
        parser.add_argument(
            "--headed", action="store_true",
            help="Запустить Chromium не в headless (для локальной отладки)",
        )

    def handle(self, *args, **opts):
        mode = opts["mode"]
        pos = opts["params"]
        headless = not opts["headed"]
        try:
            if mode == "open":
                with KadSession(headless=headless) as kad:
                    kad._ensure_main_loaded()  # noqa: SLF001 — explicit для отладки
                    self.stdout.write(self.style.SUCCESS(
                        f"OK: kad открыт, капча не обнаружена. url={kad.driver.current_url}"
                    ))
                    return

            if mode == "search":
                if not pos:
                    raise CommandError("Нужно ФИО: kad_probe search 'Иванов И.И.'")
                fio = " ".join(pos)
                with KadSession(headless=headless) as kad:
                    hits = kad.search_by_party(fio, opts["court"])
                    self.stdout.write(json.dumps(
                        [asdict(h) for h in hits],
                        ensure_ascii=False, indent=2,
                    ))
                return

            if mode == "case":
                if not pos:
                    raise CommandError("Нужен URL: kad_probe case https://kad.arbitr.ru/Card/<uuid>")
                url = pos[0]
                with KadSession(headless=headless) as kad:
                    info = kad.parse_case(url)
                    out = {
                        "case_number": info.case_number,
                        "court_name": info.court_name,
                        "judge": info.judge,
                        "instances": info.instances,
                        "events": [asdict(e) for e in info.events],
                    }
                    self.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
                return

        except KadCaptchaRequired as exc:
            self.stdout.write(self.style.WARNING(
                f"CAPTCHA на {exc.page_url} — зайди в браузер и реши"
            ))
            sys.exit(2)
        except KadParserError as exc:
            raise CommandError(f"KadParserError: {exc}")
        except NotImplementedError as exc:
            raise CommandError(f"NotImplemented: {exc}")
