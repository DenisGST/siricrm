#/siricrm/apps/realtime/utils.py
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.template.loader import render_to_string

from apps.crm.models import Message, Client  # твои модели

channel_layer = get_channel_layer()

def push_toast(user, text: str, level: str = "info"):
    """
    Тост одному пользователю (по user.id), если тебе это всё ещё нужно.
    """
    if channel_layer is None:
        return
    if not user or not user.is_authenticated:
        return

    html = render_to_string(
        "realtime/partials/toast.html",
        {
            "text": text,
            "level": level,
            "data_attr": 'data-toast="1"',  # <─ важно для фронта
        },
    )

    async_to_sync(channel_layer.group_send)(
        f"user_notifications_{user.id}",
        {"type": "notify", "html": html},
    )

def push_chat_message(msg: Message):
    if channel_layer is None:
        return

    message_html = render_to_string(
        "crm/partials/telegram_message.html",
        {
            "msg": msg,
            "data_attr": 'data-chat-message="1"',  # <─ чтобы сработал звук чата
        },
    )

    html = render_to_string(
        "realtime/partials/chat_ws_wrapper.html",
        {"inner_html": message_html},
    )

    # формируем JSON‑payload для фронта
    payload = {
        "type": "chat_message",
        "html": html,
        "client_id": str(msg.client_id),
        "client_name": (
            msg.client.first_name
            or msg.client.username
            or (msg.client.phone or "")
            or str(msg.client_id)
        ),
        "direction": msg.direction,          # incoming / outgoing
        "message_type": msg.message_type,    # text / image / document / voice ...
        "content": msg.content or "",
    }

    async_to_sync(channel_layer.group_send)(
        f"telegram_client_{msg.client_id}",
        payload,
    )

def push_client_toast(client: Client, text: str, level: str = "info"):
    """
    Показать тост всем сотрудникам, закреплённым за клиентом.
    """
    if channel_layer is None:
        return

    html = render_to_string(
        "realtime/partials/toast.html",
        {
            "text": text,
            "level": level,
            "data_attr": 'data-toast="1"',  # <─ триггер для playToastSound()
        },
    )

    async_to_sync(channel_layer.group_send)(
        f"client_ops_{client.id}",
        {"type": "notify", "html": html},
    )


def push_client_status_changed(client: Client, user):
    text = f"Статус клиента {client.first_name or client.id} изменён на «{client.get_status_display()}»"
    push_toast(user, text, level="info")
