# apps/maxchat/views.py
import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger('maxbot')


@csrf_exempt
def max_webhook(request):
    """POST-приём событий MAX Bot API. Тяжёлую обработку (скачивание вложений
    из CDN, S3, запись в БД, WS-push) НЕ делаем в ASGI-потоке — иначе под
    нагрузкой sync-threadpool daphne исчерпывается и сервер зависает (ср.
    инцидент WhatsApp 09.06.2026). Парсим JSON и ставим задачу в Celery,
    сразу отдаём 200. Дедуп по max_message_id — в самой таске."""
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

    from apps.maxchat.tasks import process_incoming_max_event
    try:
        process_incoming_max_event.delay(data)
    except Exception:
        logger.exception("MAX webhook: failed to enqueue event")

    return JsonResponse({"ok": True})

