"""Celery-задачи финансового модуля."""
import datetime

from celery import shared_task
from django.db.models import Sum

from .models import Charge


@shared_task(name="apps.finance.tasks.mark_overdue_charges")
def mark_overdue_charges():
    """Дублирует логику management-команды, чтобы запускать из beat-расписания.

    Помечает непогашенные Charge со прошедшей due_date как overdue.
    Если оплаты к этому моменту покрыли сумму — ставим paid.
    """
    today = datetime.date.today()
    candidates = Charge.objects.exclude(status="paid").filter(due_date__lt=today)
    updated = 0
    for ch in candidates:
        paid = ch.payments.filter(direction="in").aggregate(s=Sum("amount_in"))["s"] or 0
        new_status = "paid" if paid >= ch.amount else "overdue"
        if ch.status != new_status:
            ch.status = new_status
            ch.save(update_fields=["status"])
            updated += 1
    return updated
