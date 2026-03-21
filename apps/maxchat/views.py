# apps/maxchat/views.py
import json
import logging
import mimetypes
import requests

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.crm.models import Client, Message
from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3

logger = logging.getLogger(__name__)

def _determine_message_type(filename: str | None, content_type: str) -> str:
    name = filename or ""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""

    if content_type.startswith("audio/") or ext in ["ogg", "oga", "opus", "mp3", "wav", "m4a"]:
        if ext in ["ogg", "oga", "opus"] or "ogg" in content_type or "opus" in content_type:
            return "voice"
        return "audio"
    if content_type.startswith("video/") or ext in ["mp4", "avi", "mov", "mkv"]:
        return "video"
    if content_type.startswith("image/") or ext in ["jpg", "jpeg", "png", "gif", "webp"]:
        return "image"
    return "document"

@csrf_exempt
def max_webhook(request):
    try:
        raw_body = request.body.decode("utf-8")
    except UnicodeDecodeError:
        raw_body = str(request.body)

    logger.info("MAX webhook raw body: %s", raw_body)

    try:
        data = json.loads(raw_body or "{}")
    except json.JSONDecodeError:
        logger.exception("MAX webhook: invalid JSON")
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    msg = (data.get("message") or {})
    body = (msg.get("body") or {})
    sender = (msg.get("sender") or {})
    recipient = (msg.get("recipient") or {})

    text = body.get("text") or ""
    attachments = body.get("attachments") or []
    max_mid = body.get("mid") or ""

    user_id = str(sender.get("user_id") or recipient.get("chat_id") or "")
    if not user_id:
        logger.info("MAX webhook: skip, no user_id")
        return JsonResponse({"ok": True})

    client, created = Client.objects.get_or_create(
        max_chat_id=user_id,
        defaults={
            "first_name": sender.get("first_name", ""),
            "last_name": sender.get("last_name", ""),
            "username": sender.get("name", ""),
            "status": "lead",
            "last_message_at": timezone.now(),
        },
    )
    if created:
        logger.info("✨ Created MAX client %s (max_chat_id=%s)", client.id, user_id)
    else:
        logger.info("✅ Found MAX client %s (max_chat_id=%s)", client.id, user_id)
        client.last_message_at = timezone.now()
        client.save(update_fields=["last_message_at"])

      # текст
    if text:
        if max_mid and Message.objects.filter(max_message_id=max_mid, channel="max").exists():
            logger.info("MAX webhook: duplicate text mid=%s, skipping", max_mid)
        else:
            msg_obj = Message.objects.create(
                client=client,
                content=text,
                direction="incoming",
                message_type="text",
                max_message_id=max_mid,
                channel="max",
                telegram_date=timezone.now(),
                raw_payload={
                    "channel": "max",
                    "body": body,
                },
            )
            logger.info("💬 MAX text message %s for client %s", msg_obj.id, client.id)
    # вложения
    for att in attachments:
        att_type = att.get("type")
        payload = att.get("payload") or {}
        filename = att.get("filename") or None
        size = att.get("size")

        url = payload.get("url")
        if not url:
            logger.warning("MAX webhook: attachment without url, att=%r", att)
            continue
        
        # Проверка дубликата по mid + channel
        if max_mid and Message.objects.filter(
            max_message_id=max_mid,
            channel="max",
            message_type__in=["image", "video", "audio", "voice", "document"],
        ).exists():
            logger.info("MAX webhook: duplicate attachment mid=%s, skipping", max_mid)
            continue

        # качаем файл
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            file_bytes = resp.content
            content_type = (resp.headers.get("Content-Type") or "").lower()
        except Exception as e:
            logger.exception("❌ MAX webhook: failed download from %s: %s", url, e)
            continue

        if not filename:
            ext = ""
            guess_ext = mimetypes.guess_extension(content_type or "") or ""
            if guess_ext:
                ext = guess_ext.lstrip(".")
            else:
                ext = "bin"
            filename = f"max_{att_type}_{max_mid}.{ext}"

        message_type = _determine_message_type(filename, content_type)

        # заливаем в S3 (аналогично telegram/media, свой префикс)
        try:
            bucket, key = upload_file_to_s3(
                file_bytes,
                prefix="max/media",
                filename=filename,
            )
        except Exception as e:
            logger.exception("❌ MAX webhook: failed upload to S3: %s", e)
            continue

        stored = StoredFile.objects.create(
            bucket=bucket,
            key=key,
            filename=filename,
            content_type=content_type or "application/octet-stream",
            size=len(file_bytes),
        )

        msg_obj = Message.objects.create(
            client=client,
            content="",
            direction="incoming",
            message_type=message_type,
            max_message_id=max_mid,
            channel="max",
            telegram_date=timezone.now(),
            file=stored,
            file_url="",
            file_name=filename,
            raw_payload={
                "channel": "max",
                "attachment_type": att_type,
                "payload": payload,
                "size": size,
            },
        )


        logger.info(
            "📎 MAX incoming %s for client %s: msg=%s, file=%s (%d bytes)",
            message_type,
            client.id,
            msg_obj.id,
            filename,
            len(file_bytes),
        )

    return JsonResponse({"ok": True})
