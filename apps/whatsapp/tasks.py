"""Celery-задачи для отправки исходящих WhatsApp-сообщений через 1msg.io.

Логика по аналогии с ``apps/maxchat/tasks.py``:
* находим Message по id, защищаемся от двойной отправки (``is_sent``);
* если у сообщения есть прикреплённый файл — генерируем S3 pre-signed URL
  (1msg сам скачает файл с него, нам не нужно гонять байты);
* вызываем ``sender.send_whatsapp_message``;
* по успеху — фиксируем ``whatsapp_message_id`` + ``is_sent`` + ``sent_at``;
* по ошибке — retry (до 3 раз) + WS-toast сотруднику.
"""
import logging

from celery import shared_task

from apps.crm.models import Message
from apps.whatsapp.sender import send_whatsapp_message

logger = logging.getLogger("whatsapp")


def _client_whatsapp_phone(client) -> str:
    """Достаём WA-номер: сначала ClientPhone(purpose=whatsapp/primary),
    потом legacy ``Client.whatsapp_phone``, потом ``Client.phone``."""
    # Импорт лениво — phone_utils тянет много.
    from apps.crm.phone_utils import normalize_phone

    for purpose in ("whatsapp", "primary"):
        cp = client.phones.filter(purpose=purpose).first()
        if cp and cp.phone:
            return normalize_phone(cp.phone)

    if client.whatsapp_phone:
        return normalize_phone(client.whatsapp_phone)
    if client.phone:
        return normalize_phone(client.phone)
    return ""


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_whatsapp_message_task(self, message_id: str):
    try:
        msg = Message.objects.select_related("client", "file", "reply_to").get(id=message_id)
    except Message.DoesNotExist:
        logger.warning("WA task: message %s not found", message_id)
        return

    if msg.is_sent:
        return

    client = msg.client
    phone = _client_whatsapp_phone(client)
    if not phone:
        logger.warning("WA task: client %s has no whatsapp phone", client.id)
        return

    file_url = None
    filename = None
    if msg.file:
        try:
            from apps.files.s3_utils import get_presigned_url
            file_url = get_presigned_url(
                msg.file.bucket, msg.file.key, expiration=3600,
                content_type=msg.file.content_type or None,
                filename=msg.file.filename or None,
            )
            filename = msg.file.filename
        except Exception:
            logger.exception("WA task: presigned URL failed for msg %s", msg.id)

    reply_wamid = ""
    if msg.reply_to and msg.reply_to.whatsapp_message_id:
        reply_wamid = msg.reply_to.whatsapp_message_id

    ok, wamid, err = send_whatsapp_message(
        phone=phone,
        text=msg.content or "",
        file_url=file_url,
        filename=filename,
        message_type=msg.message_type or "text",
        reply_to_wamid=reply_wamid,
    )

    if ok:
        from django.utils import timezone
        msg.whatsapp_message_id = wamid or msg.whatsapp_message_id
        msg.is_sent = True
        msg.sent_at = timezone.now()
        msg.save(update_fields=["whatsapp_message_id", "is_sent", "sent_at"])
        logger.info("WA task: msg %s sent, wamid=%s", msg.id, wamid)

        try:
            from apps.realtime.utils import push_message_status, push_toast
            push_message_status(msg)
            if msg.employee and msg.employee.user:
                push_toast(msg.employee.user, "WhatsApp: отправлено", level="success")
        except Exception:
            logger.exception("WA task: WS push failed for msg %s", msg.id)
        return

    # ошибка
    logger.error("WA task: send error for msg %s: %s", msg.id, err)
    if err == "test_mode_skip":
        # TEST_MODE — это намеренный отказ, не retry
        return
    try:
        from apps.realtime.utils import push_toast
        if msg.employee and msg.employee.user:
            push_toast(msg.employee.user, f"WhatsApp: ошибка отправки — {err}", level="error")
    except Exception:
        logger.exception("WA task: toast failed for msg %s", msg.id)
    try:
        self.retry(exc=Exception(err or "wa send failed"))
    except self.MaxRetriesExceededError:
        logger.error("WA task: max retries exceeded for msg %s", msg.id)
