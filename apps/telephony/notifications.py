"""Всплывашка о входящем звонке — пуш сотруднику по WebSocket.

Идём тем же путём, что остальные оповещения проекта: в личную группу
``user_notifications_{user_id}`` уходит HTML-фрагмент с data-атрибутом, а JS
в ``dashboard.html`` на него реагирует (как с ``data-toast``,
``data-chat-message``, ``data-notification-new``).
"""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def _send(employee, html: str):
    if employee is None or not getattr(employee, "user_id", None):
        return
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            f"user_notifications_{employee.user_id}",
            {"type": "notify", "html": html},
        )
    except Exception:
        # Оповещение не должно ронять слушателя событий АТС.
        logger.debug("не удалось отправить всплывашку", exc_info=True)


def push_incoming_call(employee, *, channel_key: str, phone: str, client=None):
    """Звонит телефон сотрудника — показать карточку звонящего."""
    _send(employee, render_to_string("telephony/partials/_incoming_call.html", {
        "channel_key": channel_key,
        "phone": phone,
        "client": client,
    }))


def push_call_ended(employee, *, channel_key: str):
    """Трубку сняли или звонок сорвался — убрать всплывашку."""
    _send(employee, render_to_string("telephony/partials/_incoming_call_end.html", {
        "channel_key": channel_key,
    }))
