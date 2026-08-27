"""Результат звонка: всплывающая модалка у оператора и её обвязка.

Поток такой. Звонок завершился → слушатель AMI (``pbx_ami_listen``) зовёт
``open_prompt`` → заводится ``CallOutcome`` и в личную группу сотрудника
уходит маркер по WebSocket → JS в ``dashboard.html`` подтягивает модалку.
Оператор выбирает результат из справочника, при желании пишет комментарий и
тут же планирует следующее действие.

🛑 Запись заводится ДО того, как появится ``telephony.Call``: CDR приезжает
с АТС пачкой уже после разговора. Ключ — нога звонка (тот же channel_key,
что у карточки «вам звонили»), а ссылка на ``Call`` проставляется позже,
когда CDR доедет (``link_call``).

🛑 Модалку показываем только операторам колл-центра
(``can_access_callcenter``): у юриста после каждого разговора она была бы
помехой, а не помощью.
"""
from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone

logger = logging.getLogger(__name__)

# Звонок старше этого результат уже не спрашивает: CDR мог доехать пачкой
# после долгой недоступности CRM, и модалки посыпались бы за прошлую неделю.
MAX_PROMPT_AGE_MINUTES = 60


def _wants_prompt(employee) -> bool:
    """Спрашивать ли у этого сотрудника результат звонка.

    🛑 Строго по флагу ``Employee.can_access_callcenter``, а НЕ через
    ``can_access_callcenter(user)``: та функция пускает на доску ещё и всё
    руководство (admin / head_dep / managing_partner) — им нужен обзор
    чужих канбанов, но всплывающая после каждого разговора модалка была бы
    помехой. Результат спрашиваем только у тех, кому флаг выдан явно.
    """
    return bool(employee and getattr(employee, "can_access_callcenter", False))


def _push(employee, outcome) -> None:
    """Толкнуть маркер в личную группу сотрудника.

    Отдаём только id: саму модалку браузер запросит сам (htmx), иначе её
    пришлось бы собирать в процессе слушателя АТС, где нет ни request,
    ни csrf.
    """
    layer = get_channel_layer()
    if layer is None:
        return
    html = (f'<div data-call-result-push="{outcome.id}" style="display:none"></div>')
    try:
        async_to_sync(layer.group_send)(
            f"user_notifications_{employee.user_id}",
            {"type": "notify", "html": html},
        )
    except Exception:  # noqa: BLE001 — пуш не должен ронять слушателя АТС
        logger.debug("колл-центр: маркер результата не ушёл", exc_info=True)


def open_prompt(employee, *, channel_key: str, direction: str, phone: str = "",
                client=None, answered: bool = False, started_at=None):
    """Завести запись о звонке и показать оператору модалку. → CallOutcome | None.

    Идемпотентно по ``channel_key``: повторное событие AMI (или пришедший
    следом CDR) вторую модалку не открывает.
    """
    from .models import CallOutcome

    try:
        if employee is None or not channel_key or not _wants_prompt(employee):
            return None

        when = started_at or timezone.now()
        outcome, created = CallOutcome.objects.get_or_create(
            channel_key=channel_key[:80],
            defaults={
                "employee": employee, "client": client, "direction": direction,
                "phone": (phone or "")[:32], "answered": answered, "started_at": when,
            },
        )
        if not created:
            return outcome
        # Старьё показывать бессмысленно — запись остаётся, модалка не всплывает.
        if (timezone.now() - when).total_seconds() <= MAX_PROMPT_AGE_MINUTES * 60:
            _push(employee, outcome)
        return outcome
    except Exception:  # noqa: BLE001 — телефония важнее модалки
        logger.exception("колл-центр: не удалось открыть запрос результата (%s)",
                         channel_key)
        return None


def link_call(call) -> None:
    """Привязать строку CDR к записи результата и завести её, если модалки не было.

    Слушатель AMI ловит не всё (контейнер мог быть перезапущен, звонок мог
    не дойти до трубки), а CDR приходит на каждый звонок — это второй,
    страховочный источник. Модалку по нему тоже показываем, если звонок
    свежий: оператор ещё помнит разговор.
    """
    from .models import CallOutcome

    try:
        if call.employee_id is None:
            return
        direction = (CallOutcome.DIRECTION_IN if call.direction == "incoming"
                     else CallOutcome.DIRECTION_OUT)
        # Ключ по звонку целиком: у CDR ноги уже слиты в одну запись.
        key = f"{call.uniqueid}:{call.extension or '?'}"
        existing = CallOutcome.objects.filter(channel_key=key).first()
        if existing is not None:
            if existing.call_id is None:
                CallOutcome.objects.filter(pk=existing.pk).update(call=call)
            return

        outcome = open_prompt(
            call.employee, channel_key=key, direction=direction,
            phone=call.counterparty_phone, client=call.client,
            answered=bool(call.billsec), started_at=call.started_at,
        )
        if outcome is not None:
            CallOutcome.objects.filter(pk=outcome.pk).update(call=call)
    except Exception:  # noqa: BLE001 — приём звонков с АТС важнее
        logger.exception("колл-центр: не удалось связать звонок %s с результатом",
                         getattr(call, "uniqueid", "?"))


def pending_count(employee) -> int:
    """Сколько звонков этого сотрудника ждут результата."""
    from .models import CallOutcome

    if employee is None:
        return 0
    return CallOutcome.objects.filter(employee=employee, filled_at__isnull=True).count()
