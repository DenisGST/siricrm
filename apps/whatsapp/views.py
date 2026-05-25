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
import json
import logging

from django.http import JsonResponse, HttpResponseForbidden
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

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
    if not content and msg_type != "text":
        # медиа без caption — плейсхолдер, файл скачаем на этапе 2
        content = "(медиа)"

    msg_obj = Message.objects.create(
        client=client,
        content=content,
        direction="incoming",
        message_type=msg_type,
        channel="whatsapp",
        whatsapp_message_id=wamid,
        telegram_date=timezone.now(),
        reply_to=reply_to,
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
