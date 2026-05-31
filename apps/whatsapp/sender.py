"""HTTP-клиент к 1msg.io для исходящих WhatsApp-сообщений.

1msg.io — российский провайдер WhatsApp API (преемник chat-api.com),
URL-формат: ``{API_BASE}/{INSTANCE_ID}/<method>?token=<API_TOKEN>``.

Поддерживаются:
* ``sendMessage`` — текст (опц. quotedMsgId для ответа-цитаты);
* ``sendFile`` — документ/изображение/видео по публичному URL (S3 pre-signed);
* ``sendPTT``  — голосовое (.ogg/.opus) по URL.

Защита боевого номера: ``TEST_MODE`` + ``ALLOWED_PHONES`` (см. ``config.py``).
Если номер не в allow-list — функция возвращает (False, None, 'test_mode_skip').

Возвращаемый кортеж: ``(ok: bool, wamid: str | None, err: str | None)``.
"""
import logging
from typing import Optional, Tuple

import requests

from apps.whatsapp import config as wa_conf

logger = logging.getLogger("whatsapp")


_MEDIA_TYPE_TO_METHOD = {
    "image": "sendFile",
    "video": "sendFile",
    "audio": "sendFile",
    "document": "sendFile",
    "voice": "sendPTT",  # голосовое — отдельный метод
}


def _endpoint(method: str) -> str:
    return f"{wa_conf.API_BASE}/{wa_conf.INSTANCE_ID}/{method}?token={wa_conf.API_TOKEN}"


def _post(method: str, payload: dict, timeout: int = 30) -> Tuple[bool, dict, Optional[str]]:
    """Сырая обёртка над requests.post: возвращает (ok, json, err)."""
    if not wa_conf.is_configured():
        return False, {}, "1msg не настроен (INSTANCE_ID/API_TOKEN пусты)"

    url = _endpoint(method)
    try:
        r = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        logger.exception("WA %s: request failed: %s", method, e)
        return False, {}, f"network: {e}"

    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text}

    if r.status_code >= 400:
        logger.error("WA %s: HTTP %s — %s", method, r.status_code, data)
        return False, data, f"http {r.status_code}: {data}"

    # 1msg на успехе обычно отдаёт {"sent": true, "id": "<wamid>"} либо
    # {"sent": false, "message": "<reason>"} даже с HTTP 200.
    if data.get("sent") is False:
        return False, data, data.get("message") or "sent=false"

    return True, data, None


def send_whatsapp_message(
    phone: str,
    text: str = "",
    file_url: Optional[str] = None,
    filename: Optional[str] = None,
    message_type: str = "text",
    reply_to_wamid: str = "",
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Отправить сообщение через 1msg.io.

    Параметры:
        phone — E.164 без «+» (например, ``79991234567``).
        text — текст сообщения / caption для медиа.
        file_url — публичная ссылка на файл (S3 pre-signed). 1msg качает сам.
        filename — имя файла (для sendFile/sendPTT).
        message_type — ``text|image|video|audio|voice|document``.
        reply_to_wamid — wamid цитируемого сообщения (Meta).

    Возвращает ``(ok, wamid, err)``.
    """
    if not phone:
        return False, None, "empty phone"

    if not wa_conf.is_phone_allowed(phone):
        logger.info("WA send: TEST_MODE skip phone=%s (not in allow-list)", phone)
        return False, None, "test_mode_skip"

    # Текст
    if message_type == "text" or (not file_url and not message_type.startswith(("image", "video", "audio", "voice", "document"))):
        payload = {"phone": phone, "body": text or ""}
        if reply_to_wamid:
            payload["quotedMsgId"] = reply_to_wamid
        ok, data, err = _post("sendMessage", payload)
        return ok, (data.get("id") if ok else None), err

    # Медиа — нужен URL
    if not file_url:
        return False, None, f"file_url required for {message_type}"

    method = _MEDIA_TYPE_TO_METHOD.get(message_type, "sendFile")
    payload = {
        "phone": phone,
        "body": file_url,
        "filename": filename or "file.bin",
    }
    if text and method == "sendFile":
        payload["caption"] = text
    if reply_to_wamid:
        payload["quotedMsgId"] = reply_to_wamid
    ok, data, err = _post(method, payload, timeout=60)
    return ok, (data.get("id") if ok else None), err


def download_media(url: str, timeout: int = 60) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Скачать входящий медиафайл по URL из webhook (1msg отдаёт прямую
    ссылку в поле ``body``). Возвращает ``(bytes, content_type, err)``."""
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("WA download_media: %s — %s", url, e)
        return None, None, str(e)
    return r.content, (r.headers.get("Content-Type") or "").lower(), None
