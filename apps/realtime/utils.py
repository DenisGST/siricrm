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
        "message_id": str(msg.id),          #
        "is_sent": msg.is_sent, 
    }

    async_to_sync(channel_layer.group_send)(
        f"telegram_client_{msg.client_id}",
        {
            "type": "chat_message",
            "html": html,
            "message_id": str(msg.id),
            "is_sent": msg.is_sent,
        },
    )

def push_client_toast(client: Client, text: str, level: str = "info"):
    """
    Показать тост всем сотрудникам, закреплённым за клиентом.
    Если у клиента нет назначенных сотрудников — рассылаем всем онлайн.
    """
    if channel_layer is None:
        return

    html = render_to_string(
        "realtime/partials/toast.html",
        {
            "text": text,
            "level": level,
            "data_attr": 'data-toast="1"',
        },
    )

    has_employees = client.employees.exists()

    if has_employees:
        async_to_sync(channel_layer.group_send)(
            f"client_ops_{client.id}",
            {"type": "notify", "html": html},
        )
    else:
        # Новый клиент без куратора — уведомляем всех сотрудников
        async_to_sync(channel_layer.group_send)(
            "all_employees_notifications",
            {"type": "notify", "html": html},
        )


def push_client_status_changed(client: Client, user):
    text = f"Статус клиента {client.first_name or client.id} изменён на «{client.get_status_display()}»"
    push_toast(user, text, level="info")


def push_message_reactions(msg: Message):
    """
    Обновляет реакции на сообщение через WS.
    Шлёт JSON — не HTML. Обрабатывается JS-обработчиком.
    """
    if channel_layer is None:
        return

    async_to_sync(channel_layer.group_send)(
        f"telegram_client_{msg.client_id}",
        {
            "type": "chat_message_reactions",
            "message_id": str(msg.id),
            "reactions": msg.reactions,
        },
    )


def push_messenger_status_update(client: Client):
    """
    Шлёт OOB-обновление иконки статуса мессенджера во все места отображения
    (список клиентов в сайдбаре, карточка Kanban, бейдж в шапке чата).

    Обновление рассылается персонально каждому закреплённому сотруднику,
    т.к. статус у каждого свой.
    """
    if channel_layer is None:
        return

    from apps.crm.models import ClientEmployee

    for ce in ClientEmployee.objects.filter(client=client).select_related('employee__user'):
        html = render_to_string("crm/partials/messenger_status_oob.html", {
            "client": client, "status": ce.messenger_status,
        })
        async_to_sync(channel_layer.group_send)(
            f"user_notifications_{ce.employee.user.id}",
            {"type": "notify", "html": html},
        )


def push_message_status(msg: Message):
    """
    Обновляет статус пузыря в чате через WS.
    Шлёт JSON — не HTML. Обрабатывается отдельным JS-обработчиком.
    """
    if channel_layer is None:
        return

    async_to_sync(channel_layer.group_send)(
        f"telegram_client_{msg.client_id}",
        {
            "type": "chat_message_status",
            "message_id": str(msg.id),
            "is_sent": msg.is_sent,
        },
    )
