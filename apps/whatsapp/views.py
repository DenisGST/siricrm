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

from django.http import (
    JsonResponse, HttpResponse, HttpResponseForbidden, Http404,
)
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.crm.models import Message
from apps.whatsapp import config as wa_conf
from apps.whatsapp.processing import normalize_phone as _normalize_phone

logger = logging.getLogger("whatsapp")


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

    # Отдаём максимально «голый» ответ: Meta-WhatsApp Cloud отвергал наш
    # прокси с любым лишним заголовком (Vary/HSTS/X-Frame/Content-Disposition).
    # Adobe/picsum дают чистые Content-Type + Content-Length — копируем это.
    ctype = f.content_type or "application/octet-stream"
    if request.method == "HEAD":
        resp = HttpResponse(b"", content_type=ctype)
        if f.size:
            resp["Content-Length"] = str(f.size)
        _strip_proxy_headers(resp)
        return resp

    data = download_file_from_s3(f.bucket, f.key)
    resp = HttpResponse(data, content_type=ctype)
    resp["Content-Length"] = str(len(data))
    _strip_proxy_headers(resp)
    return resp


def _strip_proxy_headers(resp):
    """Убираем security/cookie-заголовки, которые WhatsApp Cloud не любит
    при media upload. Оставляем только Content-Type + Content-Length."""
    for h in (
        "Vary", "Set-Cookie", "X-Frame-Options", "X-Content-Type-Options",
        "Referrer-Policy", "Cross-Origin-Opener-Policy",
        "Content-Disposition", "Cache-Control", "Expires", "Pragma",
    ):
        if h in resp:
            del resp[h]


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
                # 1msg.io шлёт ack-статусы (sent/delivered/read) ключом "ack",
                # а не "statuses" — без этого прочтения/доставка не обновлялись.
                statuses.extend(value.get("ack") or [])
                for c in value.get("contacts") or []:
                    wa_id = c.get("wa_id") or c.get("waId")
                    if wa_id:
                        contacts_by_wa_id[_normalize_phone(wa_id)] = c
    else:
        if isinstance(data.get("messages"), list):
            messages.extend(data["messages"])
        if isinstance(data.get("statuses"), list):
            statuses.extend(data["statuses"])
        # 1msg.io flat-формат: {"ack":[{"id":..,"status":"read"}], "instanceId":..}
        if isinstance(data.get("ack"), list):
            statuses.extend(data["ack"])
        # одиночное событие
        if "from" in data and "id" in data:
            messages.append(data)
        # flat contacts (1msg-style)
        for c in data.get("contacts") or []:
            wa_id = c.get("wa_id") or c.get("waId")
            if wa_id:
                contacts_by_wa_id[_normalize_phone(wa_id)] = c

    # Тяжёлую обработку (скачивание медиа в S3, запись в БД, lead-routing,
    # WS-push) НЕ делаем в ASGI-потоке — иначе под нагрузкой sync-threadpool
    # daphne исчерпывается и сервер зависает (инцидент 09.06.2026). Ставим
    # задачи в Celery и сразу отдаём 200. Дедуп по wamid — в самих тасках.
    from apps.whatsapp.tasks import process_incoming_wa_message, process_wa_status

    for m in messages:
        try:
            process_incoming_wa_message.delay(m, contacts_by_wa_id)
        except Exception:
            logger.exception("WA webhook: failed to enqueue incoming message")

    for s in statuses:
        try:
            process_wa_status.delay(s)
        except Exception:
            logger.exception("WA webhook: failed to enqueue status update")

    return JsonResponse({"ok": True})

