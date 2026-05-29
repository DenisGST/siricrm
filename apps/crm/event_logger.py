"""
Утилиты для записи событий мессенджера в лог клиента.

Используется ClientLogEntry (новая модель, см. client_log.py). Старая
ClientEvent дропнута в миграции 0072.

Правила:
- Текст:       одна запись «Начат диалог» в сутки на клиента + канал.
- Файл/медиа:  отдельное событие «Получен файл» / «Отправлен файл»
               (event) или действие «Отправка файла» (action) при каждом
               сообщении.
"""
from django.utils import timezone

from apps.crm import client_log


TEXT_TYPES = {"text"}


def log_messenger_message(client, message_obj, employee=None):
    """
    Запись события при входящем или исходящем сообщении из мессенджера.

    :param client:      экземпляр Client
    :param message_obj: экземпляр Message
    :param employee:    экземпляр Employee (или None для входящих)
    """
    from apps.crm.models import ClientLogEntry, EventType

    direction = message_obj.direction   # "incoming" | "outgoing"
    channel   = message_obj.channel or "messenger"
    msg_type  = message_obj.message_type or "text"
    is_text   = msg_type in TEXT_TYPES
    label     = _channel_label(channel)

    if is_text:
        # Одна запись «Начат диалог» в сутки на клиента + канал
        today = timezone.localdate()
        already = ClientLogEntry.objects.filter(
            client=client,
            kind="event",
            event_type__code="dialog_started",
            comment__startswith=label,
            created_at__date=today,
        ).exists()
        if already:
            return

        preview = (message_obj.content or "")[:10]
        desc = (
            f"{label}: отправлено — «{preview}»"
            if direction == "outgoing"
            else f"{label}: получено — «{preview}»"
        )
        client_log.record_event(
            client, "dialog_started",
            comment=desc,
            employee=employee if direction == "outgoing" else None,
        )

    else:
        # Файл / медиа — отдельно. Входящий = event, исходящий = action.
        filename = getattr(message_obj, "file_name", "") or msg_type
        if direction == "outgoing":
            client_log.record_action(
                client, "file_sent",
                comment=f"{label}: отправлен файл — {filename}",
                employee=employee,
            )
        else:
            client_log.record_event(
                client, "file_received",
                comment=f"{label}: получен файл — {filename}",
                employee=None,
            )


def log_dialog_ended(client, employee=None, channel="мессенджер"):
    """Запись события «Окончен диалог»."""
    client_log.record_event(
        client, "dialog_ended",
        comment=f"{_channel_label(channel)}: диалог закрыт",
        employee=employee,
    )


def _channel_label(channel: str) -> str:
    return {
        "telegram": "Telegram",
        "max": "MAX",
    }.get(channel, channel.capitalize())
