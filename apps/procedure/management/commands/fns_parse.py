"""Прогон парсера справки ФНС по файлу — для проверки на реальных PDF.

    python manage.py fns_parse OLD/ФНС/КАРДАНОВ.pdf
    python manage.py fns_parse OLD/ФНС/*.pdf --json

Ничего не сохраняет: только показывает, что распознано (как в UI-логе).
"""
import glob
import json

from django.core.management.base import BaseCommand

from apps.procedure import fns_parser


class Command(BaseCommand):
    help = "Разобрать пакет ответов ФНС (PDF) и показать распознанное"

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+", help="Путь(и) к PDF, можно с маской")
        parser.add_argument("--json", action="store_true", help="Выдать сырой результат JSON")

    def handle(self, *args, **opts):
        paths = [p for pattern in opts["paths"] for p in sorted(glob.glob(pattern))]
        if not paths:
            self.stderr.write("Файлы не найдены")
            return

        for path in paths:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {path}"))
            data = open(path, "rb").read()
            result = None
            try:
                for event in fns_parser.parse_stream(data):
                    if "log" in event:
                        mark = "✅" if event.get("ok") else ("⚠️ " if event.get("warn") else "·")
                        self.stdout.write(f"  {mark} {event['log']}")
                    else:
                        result = event["result"]
            except fns_parser.FnsParseError as exc:
                self.stdout.write(self.style.ERROR(f"  ✖ {exc}"))
                continue

            if opts["json"]:
                self.stdout.write(json.dumps(result, ensure_ascii=False, indent=1))
                continue

            for acc in result["accounts"]:
                self.stdout.write(
                    f"     {acc['number']:<22} {acc['state'] or '?':<9} "
                    f"откр {acc['opened_date'] or '—'} закр {acc['closed_date'] or '—':<10} "
                    f"{acc['account_kind'][:22]:<22} {acc['bank_name'][:40]}"
                )
            for obj in result["realty"] + result["land"]:
                self.stdout.write(f"     🏠 {obj.get('object_type') or obj.get('category')} · "
                                  f"{obj['cadastral_number']} · {obj['cadastral_value']} · {obj['address'][:60]}")
            for v in result["vehicles"]:
                self.stdout.write(f"     🚗 {v['year']} {v['model']} · {v['plate']} · {v['vin']} "
                                  f"· владение до {v['dereg_date'] or '—'}")
            for c in result["incomes"]:
                self.stdout.write(f"     💰 2-НДФЛ {c['year']}: {c['total_income']} ₽ · {c['agent_name'][:50]}")
