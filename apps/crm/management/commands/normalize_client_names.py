"""
Нормализация ФИО клиентов через DaData Clean API.

qc=0 — распознано уверенно, qc=1 — частично (принимаем), qc=2 — не распознано.
Клиентам с qc=2 ставим статус "unknown".
"""
import time
import requests
from django.core.management.base import BaseCommand
from django.conf import settings
from apps.crm.models import Client


CLEAN_URL = "https://cleaner.dadata.ru/api/v1/clean/name"
PAUSE = 0.15  # секунд между запросами (тариф: только по одному)


def clean_one_name(value: str, api_key: str, secret_key: str) -> dict:
    resp = requests.post(
        CLEAN_URL,
        json=[value],
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Token {api_key}",
            "X-Secret": secret_key,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()[0]


class Command(BaseCommand):
    help = "Нормализовать ФИО всех клиентов через DaData Clean API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Только показать ч��о будет изменено, не сохранять",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        api_key = getattr(settings, "DADATA_API_KEY", "")
        secret_key = getattr(settings, "DADATA_SECRET_KEY", "")

        if not api_key or not secret_key:
            self.stderr.write(self.style.ERROR("Нужны DADATA_API_KEY и DADATA_SECRET_KEY в settings"))
            return

        clients = list(Client.objects.all().order_by("id"))
        total = len(clients)
        self.stdout.write(f"Клиентов всего: {total}")
        if dry_run:
            self.stdout.write(self.style.WARNING("-- DRY RUN, изменения не сохраняются --"))

        updated = unrecognized = skipped = errors = 0

        for i, client in enumerate(clients, 1):
            parts = [client.last_name, client.first_name, client.patronymic]
            raw = " ".join(p.strip() for p in parts if p and p.strip())

            if not raw:
                skipped += 1
                continue

            try:
                res = clean_one_name(raw, api_key, secret_key)
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  [{i}/{total}] ошибка для {repr(raw)}: {e}"))
                errors += 1
                time.sleep(1)
                continue

            qc         = res.get("qc", 2)
            surname    = (res.get("surname") or "").strip()
            name       = (res.get("name") or "").strip()
            patronymic = (res.get("patronymic") or "").strip()

            if qc == 2 or (not surname and not name):
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(f"  [{i}/{total}] {repr(raw)} → не распознано, статус→unknown")
                    )
                else:
                    if client.status != "unknown":
                        client.status = "unknown"
                        client.save(update_fields=["status"])
                unrecognized += 1
            else:
                changed = []
                if client.last_name != surname:
                    changed.append(("last_name", client.last_name, surname))
                if client.first_name != name:
                    changed.append(("first_name", client.first_name, name))
                if client.patronymic != patronymic:
                    changed.append(("patronymic", client.patronymic, patronymic))

                if not changed:
                    skipped += 1
                else:
                    if dry_run:
                        self.stdout.write(
                            f"  [{i}/{total}] qc={qc} {repr(raw)}\n"
                            + "".join(f"    {f}: {repr(o)} → {repr(n)}\n" for f, o, n in changed)
                        )
                    else:
                        client.last_name  = surname
                        client.first_name = name
                        client.patronymic = patronymic
                        client.save(update_fields=["last_name", "first_name", "patronymic"])
                    updated += 1

            time.sleep(PAUSE)
            if i % 25 == 0:
                self.stdout.write(f"  {i}/{total}...")

        self.stdout.write(self.style.SUCCESS(
            f"\nГотово: обновле��о={updated}, нераспознано={unrecognized}, "
            f"без изменений={skipped}, ошибок={errors}"
        ))
