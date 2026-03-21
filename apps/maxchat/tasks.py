# apps/maxchat/tasks.py
import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.crm.models import Message
from apps.maxchat.sender import send_max_message

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_max_message_task(self, message_id: str):
    try:
        msg = Message.objects.select_related("client", "file").get(id=message_id)
    except Message.DoesNotExist:
        logger.warning("MAX task: message %s not found", message_id)
        return

    if msg.is_sent:
        return

    client = msg.client
    if not client.max_chat_id:
        logger.warning("MAX task: client %s has no max_chat_id", client.id)
        return

    file_bytes = None
    filename = None

    if msg.file:
        try:
            from apps.files.s3_utils import download_file_from_s3
            file_bytes = download_file_from_s3(msg.file.bucket, msg.file.key)
            filename = msg.file.filename
            logger.info("MAX task: downloaded file %s from S3", filename)
        except Exception as e:
            logger.exception("MAX task: failed to download file from S3: %s", e)

    ok, max_id, err = send_max_message(
        access_token=settings.MAX_BOT_TOKEN,
        chat_id=client.max_chat_id,
        text=msg.content or "",
        file_bytes=file_bytes,
        filename=filename,
        message_type=msg.message_type,
    )

    if ok:
        msg.max_message_id = max_id
        msg.is_sent = True
        msg.sent_at = timezone.now()
        msg.save(update_fields=["max_message_id", "is_sent", "sent_at"])
        logger.info("MAX task: message %s sent, max_id=%s", msg.id, max_id)

        try:
            from apps.realtime.utils import push_message_status, push_toast
            push_message_status(msg)
            if msg.employee and msg.employee.user:
                push_toast(msg.employee.user, "Сообщение отправлено", level="success")
        except Exception as e:
            logger.warning("MAX task: failed WS update: %s", e)
    else:
        logger.error("MAX send error for msg %s: %s", msg.id, err)
        try:
            from apps.realtime.utils import push_toast
            if msg.employee and msg.employee.user:
                push_toast(msg.employee.user, f"Ошибка отправки MAX: {err}", level="error")
        except Exception as e:
            logger.warning("MAX task: failed toast: %s", e)
        try:
            self.retry(exc=Exception(err))
        except self.MaxRetriesExceededError:
            logger.error("MAX send: max retries exceeded for msg %s", msg.id)
