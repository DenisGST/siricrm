"""Проставить статус импортированным клиентам по статусу их услуги в Bubble.

Нужна для клиентов, импортированных до появления авто-статуса. Берёт
ProjectBFL клиента (по dolgnik), сопоставляет statusPrj → Client.status.

  python manage.py backfill_client_status
  python manage.py backfill_client_status --dry-run
"""
from django.core.management.base import BaseCommand

from apps.crm.models import Client
from apps.bubble_import import resolvers


class Command(BaseCommand):
    help = "Бэкафилл статуса клиентов по статусу их услуги в Bubble"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        qs = Client.objects.exclude(bubble_id=None)
        total = qs.count()
        updated = no_status = 0
        for c in qs.iterator():
            st = resolvers.resolve_client_status_by_man(c.bubble_id)
            if not st:
                no_status += 1
                continue
            if c.status != st:
                if not dry:
                    c.status = st
                    c.save(update_fields=["status"])
                updated += 1
        prefix = "[dry-run] " if dry else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}Всего {total}, обновлено {updated}, "
            f"без услуги/статуса {no_status}"
        ))
