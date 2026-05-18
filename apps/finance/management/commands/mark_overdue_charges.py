"""Помечает непогашенные начисления как просроченные (status=overdue).

Использует apps.finance.services.mark_overdue — там же логируется
переход status→overdue в ClientEvent (charge_overdue).

Запуск вручную или из cron:
  docker compose exec -T web python manage.py mark_overdue_charges
"""
from django.core.management.base import BaseCommand

from apps.finance.services import mark_overdue


class Command(BaseCommand):
    help = "Помечает непогашенные начисления как просроченные по due_date"

    def handle(self, *args, **opts):
        updated = mark_overdue()
        self.stdout.write(self.style.SUCCESS(f"Обновлено: {updated}"))
