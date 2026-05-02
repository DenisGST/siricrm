"""
Утилиты для записи событий в лог клиента (ClientEvent).

Правила для сообщений из мессенджера:
- Текст:       одна запись «Начат диалог» в сутки на клиента + канал.
- Файл/медиа:  отдельное событие «Получен файл» / «Отправлен файл» при каждом сообщении.
"""
from django.utils import timezone


TEXT_TYPES = {"text"}


def log_messenger_message(client, message_obj, employee=None):
    """
    Запись события при входящем или исходящем сообщении из мессенджера.

    :param client:      экземпляр Client
    :param message_obj: экземпляр Message
    :param employee:    экземпляр Employee (или None для входящих)
    """
    from apps.crm.models import ClientEvent

    direction = message_obj.direction   # "incoming" | "outgoing"
    channel   = message_obj.channel or "messenger"
    msg_type  = message_obj.message_type or "text"
    is_text   = msg_type in TEXT_TYPES
    label     = _channel_label(channel)

    if is_text:
        # Одна запись «Начат диалог» в сутки на клиента + канал
        today = timezone.localdate()
        already = ClientEvent.objects.filter(
            client=client,
            event_type="dialog_started",
            description__startswith=label,
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
        ClientEvent.objects.create(
            client=client,
            event_type="dialog_started",
            description=desc,
            employee=employee if direction == "outgoing" else None,
        )

    else:
        # Файл / медиа — отдельный тип события, каждый раз
        filename = getattr(message_obj, "file_name", "") or msg_type
        if direction == "outgoing":
            event_type = "file_sent"
            desc = f"{label}: отправлен файл — {filename}"
        else:
            event_type = "file_received"
            desc = f"{label}: получен файл — {filename}"

        ClientEvent.objects.create(
            client=client,
            event_type=event_type,
            description=desc,
            employee=employee if direction == "outgoing" else None,
        )


def log_dialog_ended(client, employee=None, channel="мессенджер"):
    """Запись события «Окончен диалог»."""
    from apps.crm.models import ClientEvent

    ClientEvent.objects.create(
        client=client,
        event_type="dialog_ended",
        description=f"{_channel_label(channel)}: диалог закрыт",
        employee=employee,
    )


def _channel_label(channel: str) -> str:
    return {
        "telegram": "Telegram",
        "max": "MAX",
    }.get(channel, channel.capitalize())
