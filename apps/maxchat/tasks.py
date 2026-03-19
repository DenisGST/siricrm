# apps/maxchat/tasks.py
import logging

from celery import shared_task
from django.conf import settings

from apps.crm.models import Message
from apps.maxchat.sender import send_max_message

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_max_message_task(self, message_id: str):
    try:
        msg = Message.objects.select_related("client").get(id=message_id)
    except Message.DoesNotExist:
        logger.warning("MAX task: message %s not found", message_id)
        return

    client = msg.client
    if not client.max_chat_id:
        logger.warning("MAX task: client %s has no max_chat_id", client.id)
        msg.notes = "Нет max_chat_id у клиента"
        msg.save(update_fields=["notes"])
        return

    ok, max_id, err = send_max_message(
        access_token=settings.MAX_BOT_TOKEN,  # положи токен в настройки [web:102]
        chat_id=client.max_chat_id,
        text=msg.content or "",
    )

    if ok:
        msg.max_message_id = max_id
        msg.is_sent = True
        msg.save(update_fields=["max_message_id", "is_sent"])
    else:
        logger.error("MAX send error for msg %s: %s", msg.id, err)
        msg.notes = f"MAX send error: {err}"
        msg.save(update_fields=["notes"])
        try:
            self.retry(exc=Exception(err))
        except self.MaxRetriesExceededError:
            logger.error("MAX send: max retries exceeded for msg %s", msg.id)
