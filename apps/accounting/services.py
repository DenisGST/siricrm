"""Логика разнесения входящих платежей.

Привязка `IncomingPayment` → создание `finance.Payment` (по одному на начисление;
сумму можно разбить), гашение начислений через `Charge.paid_amount`.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.finance.models import IncomeType, IncomingAccount, Payment

from .models import IncomingPayment


def parse_amount(raw) -> Decimal:
    """«15 000,50» / «15000.5» / 15000 → Decimal. Пусто/мусор → 0."""
    if raw is None:
        return Decimal("0")
    if isinstance(raw, Decimal):
        return raw
    s = str(raw).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def default_incoming_account(source: str) -> IncomingAccount | None:
    """Счёт прихода по источнику: эквайринг → «Эквайринг», иначе р/с."""
    qs = IncomingAccount.objects.filter(is_active=True)
    if source == IncomingPayment.SOURCE_ACQUIRING:
        return qs.filter(name__iexact="Эквайринг").first()
    for name in ("Расчётный счёт", "Тинькофф"):
        acc = qs.filter(name__iexact=name).first()
        if acc:
            return acc
    return qs.filter(account_type="bank").first()


def default_income_type() -> IncomeType | None:
    """Тип дохода по умолчанию — «Оплата юруслуг», иначе первый активный."""
    qs = IncomeType.objects.filter(is_active=True)
    return qs.filter(name__icontains="юруслуг").first() or qs.first()


@transaction.atomic
def bind_incoming_payment(
    ip: IncomingPayment, *, employee, client,
    allocations: list[tuple], income_type, incoming_account,
    payment_form: str, payment_date, note: str = "",
):
    """Привязать входящий платёж к клиенту и (опц.) начислениям.

    allocations — список (Charge | None, Decimal): по одному `Payment` на запись.
    Если список пуст → один `Payment` на всю сумму без начисления (только клиент).
    Повторная привязка («Изменить») сначала откатывает прежние платежи.
    """
    # Откатываем прежнее разнесение, если было.
    _delete_created_payments(ip)

    if not allocations:
        allocations = [(None, ip.amount)]

    for charge, amount in allocations:
        if amount is None or amount <= 0:
            continue
        payment = Payment.objects.create(
            payment_date=payment_date,
            direction="in",
            amount_in=amount,
            payment_form=payment_form,
            income_type=income_type,
            incoming_account=incoming_account,
            client=client,
            service=getattr(charge, "service", None),
            charge=charge,
            created_by=employee,
            comments=f"Разнесение входящего платежа ({ip.get_source_display()}, {ip.external_id})",
        )
        ip.created_payments.add(payment)
        if charge is not None:
            charge.recalc_status()

    ip.status = IncomingPayment.STATUS_BOUND
    ip.bound_client = client
    ip.bound_by = employee
    ip.bound_at = timezone.now()
    if note:
        ip.note = note
    ip.save(update_fields=["status", "bound_client", "bound_by", "bound_at", "note", "updated_at"])
    return ip


@transaction.atomic
def mark_unidentified(ip: IncomingPayment, *, employee, note: str = ""):
    """Пометить платёж неопознанным (бухгалтер не смог определить плательщика)."""
    _delete_created_payments(ip)
    ip.status = IncomingPayment.STATUS_UNIDENTIFIED
    ip.bound_client = None
    ip.bound_by = employee
    ip.bound_at = None
    if note:
        ip.note = note
    ip.save(update_fields=["status", "bound_client", "bound_by", "bound_at", "note", "updated_at"])
    return ip


def _delete_created_payments(ip: IncomingPayment):
    """Удалить ранее созданные при привязке платежи и пересчитать начисления."""
    payments = list(ip.created_payments.all())
    charges = [p.charge for p in payments if p.charge_id]
    ip.created_payments.clear()
    for p in payments:
        p.delete()
    for ch in charges:
        ch.recalc_status()
