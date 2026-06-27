# apps/maxchat/tasks.py
import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.crm.models import Message
from apps.maxchat.sender import send_max_message

logger = logging.getLogger(__name__)


# ─── приём входящих (вынесено из ASGI-обработчика вебхука) ───────────────────

@shared_task
def process_incoming_max_event(data: dict):
    """Обработать один входящий webhook-payload MAX в фоне (скачать вложения
    в S3, создать Message, WS-push). Идемпотентно по max_message_id."""
    from apps.maxchat.processing import handle_max_event
    try:
        handle_max_event(data)
    except Exception:
        logger.exception("MAX: process_incoming_max_event failed")


@shared_task(bind=True, max_retries=None)
def retry_max_attachment_download(self, message_id: str):
    """Повторное скачивание входящего MAX-вложения, не скачавшегося с первого
    раза (сетевой/CDN/DNS-сбой). URL MAX (``i.oneme.ru``) живёт долго, поэтому
    ретраим с нарастающим бэкоффом ~час. По успеху — привязываем файл к записи
    и пушим в чат; по исчерпании — помечаем «не удалось, попросите переслать».
    """
    import mimetypes
    from apps.maxchat import processing as mp
    from apps.files.models import StoredFile
    from apps.files.s3_utils import upload_file_to_s3

    try:
        msg = Message.objects.select_related("client").get(id=message_id)
    except Message.DoesNotExist:
        return
    rp = msg.raw_payload or {}
    if not rp.get("download_pending"):
        return  # уже решено

    url = rp.get("download_url")
    file_bytes, ctype, err = mp._download_max_file(url)

    if file_bytes is not None:
        filename = msg.file_name or ""
        if not filename:
            ext = (mimetypes.guess_extension(ctype or "") or ".bin").lstrip(".")
            filename = f"max_file_{msg.max_message_id}.{ext}"
        try:
            bucket, key = upload_file_to_s3(file_bytes, prefix="max/media", filename=filename)
        except Exception:
            logger.exception("MAX retry: S3 upload failed for msg %s", msg.id)
            bucket = None
        if bucket:
            stored = StoredFile.objects.create(
                bucket=bucket, key=key, filename=filename,
                content_type=ctype or "application/octet-stream", size=len(file_bytes),
            )
            try:
                from apps.files.folder_utils import get_chat_folder
                from apps.files.models import ClientFile
                cf = get_chat_folder(msg.client, "received")
                ClientFile.objects.create(
                    folder=cf, stored_file=stored, name=filename,
                    size=len(file_bytes), content_type=ctype or "application/octet-stream",
                )
            except Exception:
                pass
            msg.file = stored
            msg.file_name = filename
            msg.message_type = mp._determine_message_type(filename, ctype or "")
            rp["download_pending"] = False
            msg.raw_payload = rp
            msg.save(update_fields=["file", "file_name", "message_type", "raw_payload"])
            mp._push(msg)
            logger.info("✅ MAX retry: msg %s downloaded (%d bytes)", msg.id, len(file_bytes))
            return

    # неудача — бэкофф (1м,5м,15м,30м,60м), затем сдаёмся
    retries = int(rp.get("download_retries", 0)) + 1
    rp["download_retries"] = retries
    BACKOFFS = [60, 300, 900, 1800, 3600]
    if retries <= len(BACKOFFS):
        msg.raw_payload = rp
        msg.save(update_fields=["raw_payload"])
        retry_max_attachment_download.apply_async((message_id,), countdown=BACKOFFS[retries - 1])
        logger.warning("⏳ MAX retry %d for msg %s, next in %ds (%s)", retries, msg.id, BACKOFFS[retries - 1], err)
    else:
        rp["download_pending"] = False
        msg.raw_payload = rp
        msg.message_type = "text"
        msg.content = "❌ Клиент прислал файл, но загрузить его не удалось. Попросите переслать ещё раз."
        msg.save(update_fields=["raw_payload", "message_type", "content"])
        mp._push(msg)
        logger.error("❌ MAX retry exhausted for msg %s (url=%s)", msg.id, (url or "")[:60])


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
            # Без этого Message остаётся is_sent=False AND is_failed=False —
            # UI показывает ⏳ «отправляется» вечно (см. инцидент 24.06.2026 с DNS).
            msg.is_failed = True
            msg.error_text = (err or "max retries exceeded")[:500]
            msg.save(update_fields=["is_failed", "error_text"])
            try:
                from apps.realtime.utils import push_message_status
                push_message_status(msg)
            except Exception:
                logger.exception("MAX task: push_message_status failed for msg %s", msg.id)
