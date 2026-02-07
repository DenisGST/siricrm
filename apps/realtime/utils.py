from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.template.loader import render_to_string

from apps.crm.models import Message, Client  # твои модели

channel_layer = get_channel_layer()


def push_chat_message(msg: Message):
    if channel_layer is None:
        return

    message_html = render_to_string(
        "crm/partials/telegram_message.html",
        {"msg": msg},
    )

    html = render_to_string(
        "realtime/partials/chat_ws_wrapper.html",
        {"inner_html": message_html},
    )

    async_to_sync(channel_layer.group_send)(
        f"telegram_client_{msg.client_id}",
        {"type": "chat_message", "html": html},
    )


def push_toast(user, text: str, level: str = "info"):
    if channel_layer is None:
        return
    if not user or not user.is_authenticated:
        return

    html = render_to_string(
        "realtime/partials/toast.html",
        {"text": text, "level": level},
    )

    async_to_sync(channel_layer.group_send)(
        f"user_notifications_{user.id}",
        {"type": "notify", "html": html},
    )


def push_client_status_changed(client: Client, user):
    text = f"Статус клиента {client.first_name or client.id} изменён на «{client.get_status_display()}»"
    push_toast(user, text, level="info")
