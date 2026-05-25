"""Общая логика распределения нового лида (TG-бот, WhatsApp-webhook, и т. п.).

Логика одна: создаём Service(БФЛ), закрепляем за сотрудниками с галкой
`accept_telegram_leads=True` (если таких нет — fallback на конкретного РОПа
Власова Евгения), ставим личный статус «Лиды из Telegram» в Мой канбан.

Источник лида (Telegram / WhatsApp / лендинг) передаётся текстом в событие.
"""
from __future__ import annotations

import logging

from django.db import transaction

from apps.core.models import Employee
from .models import (
    Client, ClientEmployee, ClientEvent, Service, ServiceName,
    ServiceCommonStatus, ServiceEmployeeStatus, ServiceEmployeeState,
)

logger = logging.getLogger("lead_routing")

LEADS_STATUS_NAME = "Лиды из Telegram"
FALLBACK_HEAD_LAST = "Власов"
FALLBACK_HEAD_FIRST = "Евгений"


def lead_recipients() -> list[Employee]:
    """Активные сотрудники с галкой accept_telegram_leads. Fallback — Власов."""
    qs = Employee.objects.filter(
        is_active=True, accept_telegram_leads=True,
    ).select_related("user")
    recipients = list(qs)
    if recipients:
        return recipients
    fallback = Employee.objects.filter(
        is_active=True,
        user__last_name__iexact=FALLBACK_HEAD_LAST,
        user__first_name__iexact=FALLBACK_HEAD_FIRST,
    ).select_related("user").first()
    return [fallback] if fallback else []


def _bfl_service_name() -> ServiceName | None:
    return ServiceName.objects.filter(short_name__iexact="БФЛ").first()


def ensure_lead_employee_status(employee: Employee) -> ServiceEmployeeStatus | None:
    """Гарантировать у сотрудника личный статус «Лиды из Telegram»."""
    sn = _bfl_service_name()
    if sn is None:
        logger.warning("Нет ServiceName=БФЛ — лид не закрепится в «Мой канбан»")
        return None
    common = (
        ServiceCommonStatus.objects.filter(service_name=sn)
        .order_by("order", "name").first()
    )
    if common is None:
        return None
    obj, _ = ServiceEmployeeStatus.objects.get_or_create(
        employee=employee, name=LEADS_STATUS_NAME,
        defaults={"common_status": common, "is_active": True, "order": 0},
    )
    return obj


@transaction.atomic
def route_new_lead(
    client: Client, source_label: str, event_description: str = ""
) -> list[Employee]:
    """Закрепить нового лида за получателями + создать Service(БФЛ) +
    зафиксировать событие. source_label — короткое имя источника
    («Telegram-бот», «WhatsApp», «Лендинг сириус-бфл.рф/clip-3n/»).
    Возвращает список получателей."""
    recipients = lead_recipients()
    if not recipients:
        logger.error("route_new_lead: нет получателей (ни галок, ни Власова)")
        ClientEvent.objects.create(
            client=client, event_type="lead_received", employee=None,
            description=(event_description
                         or f"Новый лид ({source_label}). "
                         "Получателей не найдено — назначьте вручную."),
        )
        return []

    for emp in recipients:
        ClientEmployee.objects.get_or_create(client=client, employee=emp)

    sn = _bfl_service_name()
    if sn is not None:
        service, _ = Service.objects.get_or_create(
            client=client, name=sn,
            defaults={"is_active": True},
        )
        for emp in recipients:
            emp_status = ensure_lead_employee_status(emp)
            ServiceEmployeeState.objects.update_or_create(
                service=service, employee=emp,
                defaults={"status": emp_status},
            )

    names = ", ".join(e.user.get_full_name() or e.user.username for e in recipients)
    desc = event_description or f"Новый лид ({source_label}). Распределён: {names}"
    ClientEvent.objects.create(
        client=client, event_type="lead_received", employee=None,
        description=desc,
    )
    return recipients
