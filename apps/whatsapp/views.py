"""Webhook + (в дальнейшем) UI-эндпоинты для WhatsApp-интеграции 1msg.io.

На этапе 1 реализован только приём webhook'ов.

Безопасность на боевом номере (см. apps.whatsapp.config):
* В TEST_MODE пишем в БД только сообщения от номеров из allow-list, для
  остальных возвращаем 200 OK и пишем в лог (1msg перестанет ретраить).
* Опциональный shared-secret в URL ``/webhook/whatsapp/<secret>/`` — если
  ``WHATSAPP_WEBHOOK_SECRET`` задан, без совпадения 403.

Скачивание медиа из Meta — отложено до этапа 2 (нужны рабочие
1msg-эндпоинты). Пока во входящих медиа создаётся Message с типом
``document`` и пустым file — текст-плейсхолдер «(медиа)».
"""
import datetime
import json
import logging

from django.http import JsonResponse, HttpResponseForbidden, StreamingHttpResponse, Http404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.crm.models import Client, Message
from apps.whatsapp import config as wa_conf

logger = logging.getLogger("whatsapp")


# ─── helpers ───────────────────────────────────────────────


def _normalize_phone(raw: str) -> str:
    """Превратить любой формат (с +, с @c.us) в E.164 без +."""
    if not raw:
        return ""
    s = str(raw).strip()
    if s.endswith("@c.us"):
        s = s[:-5]
    return s.lstrip("+").strip()


def _push(msg_obj):
    try:
        from apps.realtime.utils import push_chat_message
        push_chat_message(msg_obj)
    except Exception as e:
        logger.warning("WA webhook: failed WS push: %s", e)


def _get_or_create_wa_client(phone: str, profile_name: str = "") -> tuple[Client, bool]:
    """Найти/создать клиента по любому из его номеров (Client.whatsapp_phone
    либо ClientPhone-алиас). Если незнакомый номер — создаём лид и
    распределяем как при заявке через TG-бот."""
    from apps.crm.phone_utils import add_client_phone, find_client_by_phone
    from apps.crm.lead_routing import route_new_lead

    client = find_client_by_phone(phone, purposes=["whatsapp", "primary"])
    if client is not None:
        client.last_message_at = timezone.now()
        client.save(update_fields=["last_message_at"])
        return client, False

    first, last = "", ""
    if profile_name:
        parts = profile_name.strip().split(maxsplit=1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""

    # whatsapp_phone в legacy-поле — unique=True, ставим только если свободен.
    legacy_wa = phone if not Client.objects.filter(whatsapp_phone=phone).exists() else None
    client = Client.objects.create(
        first_name=first or "WhatsApp",
        last_name=last,
        username="",
        phone="+" + phone,
        whatsapp_phone=legacy_wa,
        status="lead",
        last_message_at=timezone.now(),
    )
    add_client_phone(client, phone, "whatsapp")
    add_client_phone(client, phone, "primary")
    logger.info("✨ WA: автосоздан лид %s (phone=%s, name=%s)",
                client.id, phone, profile_name)
    try:
        route_new_lead(
            client,
            source_label="WhatsApp",
            event_description=(
                f"Первое обращение через WhatsApp с номера +{phone}. "
                f"Профиль: «{profile_name or '—'}»."
            ),
        )
    except Exception:
        logger.exception("WA: не удалось распределить лид")
    return client, True


def _extract_text(message: dict) -> str:
    """Достать текст сообщения из payload Meta/1msg в нескольких форматах."""
    if not isinstance(message, dict):
        return ""
    # Meta Cloud API: {"type": "text", "text": {"body": "..."}}
    body = (message.get("text") or {}).get("body")
    if body:
        return body
    # 1msg legacy: {"body": "..."} или {"caption": "..."}
    return message.get("body") or message.get("caption") or ""


def _detect_message_type(message: dict) -> str:
    if not isinstance(message, dict):
        return "text"
    t = (message.get("type") or "").lower()
    if t in {"image", "video", "audio", "voice", "document", "sticker", "location", "contacts", "text"}:
        # voice / audio
        if t == "audio":
            # PTT / голосовое — у Meta флаг message["audio"]["voice"]
            audio = message.get("audio") or {}
            if audio.get("voice"):
                return "voice"
            return "audio"
        if t == "sticker":
            return "image"
        if t in {"location", "contacts"}:
            return "text"
        return t
    return "text"


# ─── webhook ───────────────────────────────────────────────


# Окно жизни прокси-ссылок на исходящие медиа: после этого периода
# WhatsApp Cloud уже скачал и зеркалит файл сам, дальше не приходит.
_WA_PROXY_TTL = datetime.timedelta(hours=24)


@csrf_exempt
@require_http_methods(["GET", "HEAD"])
def wa_file_proxy(request, file_id):
    """Публичный прокси-ридер StoredFile из S3 — нужен для 1msg.io.

    Beget pre-signed URL отвечает 403 на HEAD, а 1msg перед скачиванием
    делает HEAD-probe и обрывает доставку медиа с «Media upload error».
    Стримим файл прямо со своего домена с корректным Content-Type и
    поддержкой HEAD.

    Защита: разрешаем только файлы, привязанные к WhatsApp-сообщениям
    созданным в последние 24 часа.
    """
    from apps.files.models import StoredFile
    from apps.files.s3_utils import download_file_from_s3

    try:
        f = StoredFile.objects.get(id=file_id)
    except StoredFile.DoesNotExist:
        raise Http404("not found")

    cutoff = timezone.now() - _WA_PROXY_TTL
    if not Message.objects.filter(
        file=f, channel="whatsapp", created_at__gte=cutoff,
    ).exists():
        # Файл либо не WA-шный, либо «протух». Не отдаём — иначе любой
        # знающий UUID мог бы тянуть медиа клиентов.
        raise Http404("expired or not whatsapp")

    # WhatsApp/1msg парсит Content-Disposition как plain ASCII — Django при
    # кириллице в filename подставляет RFC 2047 encoded-word, и 1msg валит
    # «Media upload error». Делаем ASCII-safe basename из file_id + extension.
    import os
    from urllib.parse import quote
    orig = f.filename or "file"
    _, ext = os.path.splitext(orig)
    ascii_name = f"{f.id}{ext}".encode("ascii", errors="ignore").decode("ascii") or "file"
    cd = f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(orig)}'

    if request.method == "HEAD":
        resp = StreamingHttpResponse(b"", content_type=f.content_type or "application/octet-stream")
        if f.size:
            resp["Content-Length"] = str(f.size)
        resp["Content-Disposition"] = cd
        return resp

    data = download_file_from_s3(f.bucket, f.key)
    resp = StreamingHttpResponse(iter([data]), content_type=f.content_type or "application/octet-stream")
    resp["Content-Length"] = str(len(data))
    resp["Content-Disposition"] = cd
    return resp


@csrf_exempt
def whatsapp_webhook(request, secret: str = ""):
    """POST-приём событий от 1msg.io. Никогда не возвращает 5xx — иначе
    1msg будет ретраить и засорять очередь. На внутренней ошибке логируем
    и отдаём 200."""
    if wa_conf.WEBHOOK_SECRET and secret != wa_conf.WEBHOOK_SECRET:
        logger.warning("WA webhook: bad secret in URL")
        return HttpResponseForbidden("bad secret")

    try:
        raw = request.body.decode("utf-8", errors="replace")
    except Exception:
        raw = str(request.body)

    logger.info("WA webhook raw body: %s", raw[:2000])

    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        logger.warning("WA webhook: invalid JSON")
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=200)

    # 1msg.io шлёт события несколькими форматами. Разворачиваем оба:
    # 1) Meta-style: {"entry": [{"changes": [{"value": {"messages": [...]}}]}]}
    # 2) flat: {"messages": [...], "statuses": [...], "ack": ...}
    messages: list[dict] = []
    statuses: list[dict] = []
    contacts_by_wa_id: dict[str, dict] = {}

    if isinstance(data, dict) and data.get("entry"):
        for entry in data.get("entry") or []:
            for change in entry.get("changes") or []:
                value = (change.get("value") or {})
                messages.extend(value.get("messages") or [])
                statuses.extend(value.get("statuses") or [])
                for c in value.get("contacts") or []:
                    wa_id = c.get("wa_id") or c.get("waId")
                    if wa_id:
                        contacts_by_wa_id[_normalize_phone(wa_id)] = c
    else:
        if isinstance(data.get("messages"), list):
            messages.extend(data["messages"])
        if isinstance(data.get("statuses"), list):
            statuses.extend(data["statuses"])
        # одиночное событие
        if "from" in data and "id" in data:
            messages.append(data)
        # flat contacts (1msg-style)
        for c in data.get("contacts") or []:
            wa_id = c.get("wa_id") or c.get("waId")
            if wa_id:
                contacts_by_wa_id[_normalize_phone(wa_id)] = c

    for m in messages:
        _handle_incoming_message(m, contacts_by_wa_id)

    for s in statuses:
        _handle_status_update(s)

    return JsonResponse({"ok": True})


def _extract_media_url_and_name(message: dict) -> tuple[str, str]:
    """Достать URL и имя файла из incoming-медиа.

    Источники по убыванию приоритета:
    1) 1msg-стиль: ``message["body"]`` — прямая https-ссылка;
    2) Meta-стиль: ``message["image"|"video"|"document"|"audio"]`` —
       объект с полями ``link`` / ``url`` / ``filename``;
    3) Meta media id (только ``id``) — НЕ поддерживается в текущей версии:
       нужен отдельный GET к медиа-endpoint Meta.
    """
    if not isinstance(message, dict):
        return "", ""

    body = message.get("body") or ""
    if isinstance(body, str) and body.startswith(("http://", "https://")):
        name = message.get("filename") or message.get("caption") or ""
        return body, name

    for key in ("image", "video", "audio", "voice", "document", "sticker"):
        obj = message.get(key) or {}
        if not isinstance(obj, dict):
            continue
        url = obj.get("link") or obj.get("url") or ""
        name = obj.get("filename") or ""
        if url:
            return url, name

    return "", ""


def _download_wa_media_to_s3(url: str, filename: str, wamid: str):
    """Скачать медиа из 1msg/Meta CDN и положить в S3 как StoredFile.
    Best-effort: при ошибке возвращает None — обработка сообщения не падает."""
    try:
        from apps.whatsapp.sender import download_media
        from apps.files.models import StoredFile
        from apps.files.s3_utils import upload_file_to_s3

        data, ctype, err = download_media(url)
        if err or not data:
            logger.warning("WA media недоступно: %s", err)
            return None

        # Если имя пустое — генерируем из wamid и расширения по content-type.
        if not filename:
            ext = ""
            if ctype:
                ext = {
                    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                    "video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
                    "application/pdf": ".pdf",
                }.get(ctype.split(";")[0].strip(), "")
            filename = (wamid or "wa_media")[:60] + ext

        bucket, key = upload_file_to_s3(
            data, prefix="whatsapp/incoming",
            filename=filename, content_type=ctype or None,
        )
        return StoredFile.objects.create(
            bucket=bucket, key=key, filename=filename[:255],
            content_type=(ctype or "")[:255], size=len(data),
            bubble_id=f"wamedia_{wamid}"[:64],
        )
    except Exception:
        logger.exception("WA media: download/upload failed for url=%s", url)
        return None


def _handle_incoming_message(message: dict, contacts: dict):
    """Один входящий message-объект из payload."""
    if not isinstance(message, dict):
        return

    phone = _normalize_phone(message.get("from") or message.get("chatId") or "")
    if not phone:
        logger.info("WA webhook: skip, no 'from'")
        return

    if not wa_conf.is_phone_allowed(phone):
        logger.info("WA webhook: TEST_MODE skip phone=%s (not in allow-list)", phone)
        return

    wamid = message.get("id") or message.get("messageId") or ""
    if wamid and Message.objects.filter(whatsapp_message_id=wamid, channel="whatsapp").exists():
        logger.info("WA webhook: duplicate wamid=%s, skipping", wamid)
        return

    profile_name = ""
    contact = contacts.get(phone)
    if contact:
        profile_name = (contact.get("profile") or {}).get("name") or ""

    client, _ = _get_or_create_wa_client(phone, profile_name)

    text = _extract_text(message)
    msg_type = _detect_message_type(message)

    # Цитата: Meta даёт message["context"]["id"] = wamid цитируемого
    reply_to = None
    ctx_id = (message.get("context") or {}).get("id")
    if ctx_id:
        reply_to = Message.objects.filter(
            whatsapp_message_id=ctx_id, channel="whatsapp",
        ).first()

    content = text

    # Медиа: 1msg в большинстве случаев кладёт прямую CDN-ссылку в
    # message["body"] для image/video/audio/document. Скачиваем в S3,
    # привязываем StoredFile к сообщению. Если не получилось — оставляем
    # текстовый плейсхолдер «(медиа)», обработка не валится.
    stored_file = None
    if msg_type != "text":
        media_url, media_name = _extract_media_url_and_name(message)
        if media_url:
            stored_file = _download_wa_media_to_s3(media_url, media_name, wamid)
        if not content:
            content = media_name or "(медиа)"

    msg_obj = Message.objects.create(
        client=client,
        content=content,
        direction="incoming",
        message_type=msg_type,
        channel="whatsapp",
        whatsapp_message_id=wamid,
        telegram_date=timezone.now(),
        reply_to=reply_to,
        file=stored_file,
        raw_payload={"channel": "whatsapp", "message": message},
    )
    logger.info("💬 WA incoming msg %s type=%s for client %s", msg_obj.id, msg_type, client.id)

    _push(msg_obj)
    try:
        from apps.crm.event_logger import log_messenger_message
        log_messenger_message(client, msg_obj)
    except Exception:
        logger.exception("WA: log_messenger_message failed")


def _handle_status_update(status: dict):
    """Ack-статус: sent / delivered / read для исходящего сообщения."""
    if not isinstance(status, dict):
        return
    wamid = status.get("id") or status.get("messageId")
    state = (status.get("status") or status.get("ack") or "").lower()
    if not wamid or not state:
        return

    msg = Message.objects.filter(whatsapp_message_id=wamid, channel="whatsapp").first()
    if not msg:
        logger.info("WA webhook: status for unknown wamid=%s state=%s", wamid, state)
        return

    updated = []
    if state in {"sent", "1"} and not msg.is_sent:
        msg.is_sent = True
        msg.sent_at = msg.sent_at or timezone.now()
        updated += ["is_sent", "sent_at"]
    if state in {"delivered", "2"} and not msg.is_delivered:
        msg.is_delivered = True
        updated.append("is_delivered")
    if state in {"read", "3"} and not msg.is_read:
        msg.is_read = True
        updated.append("is_read")

    if updated:
        msg.save(update_fields=updated)
        logger.info("WA status: msg=%s → %s", msg.id, state)
        try:
            from apps.realtime.utils import push_message_status
            push_message_status(msg)
        except Exception:
            logger.exception("WA: push_message_status failed")
