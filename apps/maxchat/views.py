# apps/maxchat/views.py
import json

from django.http import JsonResponse, HttpRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

from apps.crm.models import Client, Message
from apps.realtime.utils import push_chat_message, push_client_toast

import logging
logger = logging.getLogger("django")


@csrf_exempt
def max_webhook(request: HttpRequest):
    # Разрешаем только POST
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method_not_allowed"}, status=405)

    # Проверяем секрет из заголовка (если задан)
    expected_secret = getattr(settings, "MAX_WEBHOOK_SECRET", "")
    got_secret = request.headers.get("X-Max-Bot-Api-Secret", "")
    if expected_secret and got_secret != expected_secret:
        return JsonResponse({"ok": False, "error": "bad_secret"}, status=403)

    # Парсим JSON
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        logger.exception("MAX webhook: bad json")
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    # ---- Разбор Update ----
    # По доке MAX Update содержит поле type и payload.
    # Для message_created в payload лежит само сообщение.
    update_type = data.get("type")
    payload = data.get("payload") or {}

    if update_type != "message_created":
        # Игнорируем другие типы, но возвращаем 200, чтобы MAX не ретраил
        return JsonResponse({"ok": True})

    message_obj = payload.get("message") or payload

    # Вариант структуры:
    # {
    #   "user_id": "...",
    #   "text": "...",
    #   "id": "...",
    #   ...
    # }
    user_id = str(message_obj.get("user_id") or "")
    text = message_obj.get("text") or ""
    external_message_id = str(
        message_obj.get("id")
        or message_obj.get("message_id")
        or ""
    )

    if not user_id or not text:
        # Ничего полезного — просто подтверждаем получение
        return JsonResponse({"ok": True})

    # ---- Клиент ----
    client, _ = Client.objects.get_or_create(
        max_user_id=user_id,
        defaults={
            "first_name": "",
            "last_name": "",
            "username": "",
            "status": "lead",
            "last_message_at": timezone.now(),
        },
    )
    client.last_message_at = timezone.now()
    client.save(update_fields=["last_message_at"])

    # ---- Сообщение в CRM ----
    msg = Message.objects.create(
        client=client,
        content=text,
        message_type="text",
        direction="incoming",
        channel="max",                 # важно, чтобы отличать от telegram
        is_sent=True,
        is_read=True,
        max_message_id=external_message_id,
        telegram_message_id=None,
        telegram_date=timezone.now(),  # можно завести отдельное поле, если хочешь
    )

    # ---- WebSocket + toast ----
    # В чат
    push_chat_message(msg)

    # В тост (как сделали для Telegram)
    preview = (text[:10] + "…") if len(text) > 10 else text
    push_client_toast(
        client,
        text=f"💬 {preview} — новое MAX‑сообщение",
    )

    return JsonResponse({"ok": True})
