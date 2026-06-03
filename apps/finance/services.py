"""Финансовая бизнес-логика, разделяемая celery-task и management-командой."""
import datetime

from django.db.models import Sum

from apps.crm import client_log
from apps.crm.models import Client


def _log_charge_overdue(charge):
    """Запись события charge_overdue (system). Employee=None."""
    if not charge.client_id:
        return
    client = Client.objects.filter(pk=charge.client_id).first()
    if client is None:
        return
    client_log.record_event(
        client, "charge_overdue", employee=None,
        comment=(
            f"Просрочено начисление «{charge.title}» от "
            f"{charge.due_date.strftime('%d.%m.%Y')} на {charge.amount} руб."
        ),
    )


def mark_overdue(charge_qs=None) -> int:
    """Помечает все непогашенные просроченные Charge как overdue, логирует
    переход в ClientEvent. Возвращает число обновлённых записей."""
    from .models import Charge

    today = datetime.date.today()
    if charge_qs is None:
        charge_qs = Charge.objects.exclude(status="paid").filter(due_date__lt=today)

    updated = 0
    for ch in charge_qs:
        paid = ch.payments.filter(direction="in").aggregate(s=Sum("amount_in"))["s"] or 0
        if paid >= ch.amount:
            new_status = "paid"
        else:
            new_status = "overdue"
        if ch.status == new_status:
            continue
        was_overdue = ch.status == "overdue"
        ch.status = new_status
        ch.save(update_fields=["status"])
        updated += 1
        # Логируем только переход НА overdue (а не paid/scheduled).
        if new_status == "overdue" and not was_overdue:
            _log_charge_overdue(ch)
    return updated
