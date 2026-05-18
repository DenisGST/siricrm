"""Пометить непогашенные начисления как просроченные (status=overdue).

Выбирает Charge у которых:
  * статус не paid,
  * due_date < сегодня,
  * сумма оплаченных < amount.

Запуск вручную или из daily cron:
  docker compose exec -T web python manage.py mark_overdue_charges
"""
import datetime

from django.core.management.base import BaseCommand
from django.db.models import Sum

from apps.finance.models import Charge


class Command(BaseCommand):
    help = "Помечает непогашенные начисления как просроченные по due_date"

    def handle(self, *args, **opts):
        today = datetime.date.today()
        candidates = Charge.objects.exclude(status="paid").filter(due_date__lt=today)
        updated = 0
        for ch in candidates:
            paid = ch.payments.filter(direction="in").aggregate(s=Sum("amount_in"))["s"] or 0
            if paid >= ch.amount:
                if ch.status != "paid":
                    ch.status = "paid"
                    ch.save(update_fields=["status"])
                    updated += 1
            else:
                if ch.status != "overdue":
                    ch.status = "overdue"
                    ch.save(update_fields=["status"])
                    updated += 1
        self.stdout.write(self.style.SUCCESS(f"Обновлено: {updated}"))
