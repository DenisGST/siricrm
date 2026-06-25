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
from apps.whatsapp.sender import send_whatsapp_message, send_whatsapp_template

logger = logging.getLogger("whatsapp")


def _last_inbound_wa_phone(client) -> str:
    """Номер, с которого клиент реально пишет нам в WhatsApp — берём из
    последнего входящего WA-сообщения (chatId/author в raw_payload).

    Это надёжнее абстрактного primary: у клиента может быть несколько
    номеров (напр. отдельный для Telegram), а WhatsApp работает только на
    том, с которого идёт живой диалог. Иначе исходящие летят на номер без
    WhatsApp → Meta «Message undeliverable» (инцидент с Кириллом Мишичевым)."""
    import re
    last_in = (
        Message.objects
        .filter(client=client, channel="whatsapp", direction="incoming")
        .order_by("-created_at")
        .values_list("raw_payload", flat=True)
        .first()
    )
    if not last_in:
        return ""
    msg = (last_in or {}).get("message") or {}
    raw = msg.get("chatId") or msg.get("author") or msg.get("from") or ""
    digits = re.sub(r"\D", "", str(raw).split("@")[0])
    return digits


def _client_whatsapp_phone(client) -> str:
    """WA-номер для исходящего. Приоритет:
    1) номер последнего входящего WA (живой диалог);
    2) ClientPhone(purpose=whatsapp);
    3) ClientPhone(purpose=primary);
    4) legacy ``Client.whatsapp_phone`` → ``Client.phone``."""
    # Импорт лениво — phone_utils тянет много.
    from apps.crm.phone_utils import normalize_phone

    inbound = _last_inbound_wa_phone(client)
    if inbound:
        return normalize_phone(inbound)

    for purpose in ("whatsapp", "primary"):
        cp = client.phones.filter(purpose=purpose).first()
        if cp and cp.phone:
            return normalize_phone(cp.phone)

    if client.whatsapp_phone:
        return normalize_phone(client.whatsapp_phone)
    if client.phone:
        return normalize_phone(client.phone)
    return ""


# ─── приём входящих (вынесено из ASGI-обработчика вебхука) ───────────────────

@shared_task
def process_incoming_wa_message(message: dict, contacts: dict = None):
    """Обработать один входящий WA-message в фоне (скачать медиа в S3,
    создать Message, распределить лид, WS-push). Идемпотентно по wamid."""
    from apps.whatsapp.processing import handle_incoming_message
    try:
        handle_incoming_message(message, contacts or {})
    except Exception:
        logger.exception("WA: process_incoming_wa_message failed")


@shared_task
def process_wa_status(status: dict):
    """Обработать один ack-статус (sent/delivered/read/failed) в фоне."""
    from apps.whatsapp.processing import handle_status_update
    try:
        handle_status_update(status)
    except Exception:
        logger.exception("WA: process_wa_status failed")


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_whatsapp_template_task(self, message_id: str):
    """Отправить исходящее WA-сообщение как approved WABA-шаблон
    (``sendTemplate``) — работает вне 24-часового окна.

    Сообщение должно иметь ``message_template`` (одобренный Meta) и
    ``template_params`` (значения {{1}}…). Текст для отображения в чате
    уже отрендерен во ``content`` на этапе создания Message.
    """
    try:
        msg = Message.objects.select_related("client", "message_template").get(id=message_id)
    except Message.DoesNotExist:
        logger.warning("WA template task: message %s not found", message_id)
        return

    if msg.is_sent:
        return

    tpl = msg.message_template
    if not tpl or not tpl.whatsapp_template_name or tpl.whatsapp_meta_status != "approved":
        logger.error("WA template task: msg %s has no approved template", msg.id)
        msg.is_failed = True
        msg.error_text = "Шаблон не задан или не одобрен Meta"
        msg.save(update_fields=["is_failed", "error_text"])
        try:
            from apps.realtime.utils import push_message_status
            push_message_status(msg)
        except Exception:
            pass
        return

    phone = _client_whatsapp_phone(msg.client)
    if not phone:
        logger.warning("WA template task: client %s has no whatsapp phone", msg.client_id)
        return

    ok, wamid, err = send_whatsapp_template(
        phone=phone,
        template_name=tpl.whatsapp_template_name,
        body_params=msg.template_params or [],
        language_code=tpl.whatsapp_language or "ru",
    )

    if ok:
        from django.utils import timezone
        msg.whatsapp_message_id = wamid or msg.whatsapp_message_id
        msg.is_sent = True
        msg.sent_at = timezone.now()
        msg.save(update_fields=["whatsapp_message_id", "is_sent", "sent_at"])
        logger.info("WA template task: msg %s sent, wamid=%s", msg.id, wamid)
        try:
            from apps.realtime.utils import push_message_status, push_toast
            push_message_status(msg)
            if msg.employee and msg.employee.user:
                push_toast(msg.employee.user, "WhatsApp: шаблон отправлен", level="success")
        except Exception:
            logger.exception("WA template task: WS push failed for msg %s", msg.id)
        return

    logger.error("WA template task: send error for msg %s: %s", msg.id, err)
    if err == "test_mode_skip":
        return
    try:
        from apps.realtime.utils import push_toast
        if msg.employee and msg.employee.user:
            push_toast(msg.employee.user, f"WhatsApp: ошибка шаблона — {err}", level="error")
    except Exception:
        logger.exception("WA template task: toast failed for msg %s", msg.id)
    try:
        self.retry(exc=Exception(err or "wa template send failed"))
    except self.MaxRetriesExceededError:
        logger.error("WA template task: max retries exceeded for msg %s", msg.id)
        # Без этого Message остаётся is_sent=False AND is_failed=False —
        # UI показывает ⏳ «отправляется» вечно (инцидент 24.06.2026 с DNS).
        msg.is_failed = True
        msg.error_text = (err or "max retries exceeded")[:500]
        msg.save(update_fields=["is_failed", "error_text"])
        try:
            from apps.realtime.utils import push_message_status
            push_message_status(msg)
        except Exception:
            logger.exception("WA template task: push_message_status failed for msg %s", msg.id)


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
        # Шлём файл в 1msg как data:URI с base64 — это документированный
        # формат body параметра sendFile (наряду с URL и multipart). Это
        # надёжнее, чем гонять 1msg по нашему прокси: нет HEAD-probe,
        # нет проблем с кириллицей в URL, нет лимита на размер по URL.
        import base64
        try:
            from apps.files.s3_utils import download_file_from_s3
            data = download_file_from_s3(msg.file.bucket, msg.file.key)
            ctype = msg.file.content_type or "application/octet-stream"
            b64 = base64.b64encode(data).decode("ascii")
            file_url = f"data:{ctype};base64,{b64}"
            filename = msg.file.filename
        except Exception:
            logger.exception("WA task: failed to base64-encode file for msg %s", msg.id)

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

    # Перманентные ошибки (ретрай бессмысленен — текст/параметры не пройдут
    # и со 2-й попытки). Помечаем failed один раз, без спама ретраями.
    err_low = (err or "").lower()
    permanent = (
        "consecutive spaces" in err_low
        or "new-line/tab" in err_low
        or "invalid parameter" in err_low
    )

    from django.utils import timezone
    if permanent:
        msg.is_failed = True
        msg.error_text = "Недопустимый текст (табы или 4+ пробелов подряд)"
        msg.save(update_fields=["is_failed", "error_text"])
        try:
            from apps.realtime.utils import push_message_status, push_toast
            push_message_status(msg)
            if msg.employee and msg.employee.user:
                push_toast(msg.employee.user, "WhatsApp: недопустимый текст (табы/много пробелов)", level="error")
        except Exception:
            logger.exception("WA task: toast failed for msg %s", msg.id)
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
        # Без этого Message остаётся is_sent=False AND is_failed=False —
        # UI показывает ⏳ «отправляется» вечно (инцидент 24.06.2026 с DNS).
        msg.is_failed = True
        msg.error_text = (err or "max retries exceeded")[:500]
        msg.save(update_fields=["is_failed", "error_text"])
        try:
            from apps.realtime.utils import push_message_status
            push_message_status(msg)
        except Exception:
            logger.exception("WA task: push_message_status failed for msg %s", msg.id)
