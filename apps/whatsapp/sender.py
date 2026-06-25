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
import re
from typing import Optional, Tuple

import requests

from apps.whatsapp import config as wa_conf

logger = logging.getLogger("whatsapp")


def sanitize_wa_text(text: str) -> str:
    """Привести текст под ограничения 1msg.io: ``body``/``caption`` НЕ может
    содержать табы или >4 пробелов подряд (иначе HTTP 500 «Param text cannot
    have new-line/tab characters or more than 4 consecutive spaces»). Переносы
    строк (`\\n`) 1msg принимает — их сохраняем (многострочные сообщения)."""
    if not text:
        return text
    text = text.replace("\t", " ")        # табы → пробел
    text = text.replace("\r", "")          # CR убираем (оставляем \n)
    text = re.sub(r" {4,}", " ", text)     # 4+ пробелов подряд → один
    return text


_MEDIA_TYPE_TO_METHOD = {
    "image": "sendFile",
    "video": "sendFile",
    "audio": "sendFile",
    "document": "sendFile",
    "voice": "sendFile",  # 1msg.io отдельного sendPTT нет — голосовое тоже sendFile
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

    # 1msg отклоняет табы / >4 пробелов подряд в body/caption (HTTP 500)
    text = sanitize_wa_text(text)

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


# ─── WABA-шаблоны (sendTemplate / addTemplate / list) ────────────────────────

def send_whatsapp_template(
    phone: str,
    template_name: str,
    body_params: Optional[list] = None,
    language_code: str = "ru",
    namespace: str = "",
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Отправить approved WABA-шаблон через 1msg ``sendTemplate``.

    Работает даже вне 24-часового окна (в отличие от free-form sendMessage).

    Параметры:
        phone — E.164 без «+».
        template_name — имя шаблона в Meta (``MessageTemplate.whatsapp_meta_id``
            хранит id, но 1msg sendTemplate принимает именно ``name``).
        body_params — список строк для подстановки в {{1}}, {{2}}… по порядку.
        language_code — код языка шаблона (``ru`` / ``en`` …).
        namespace — namespace WABA (по умолчанию из config).

    Возвращает ``(ok, wamid, err)``.
    """
    if not phone:
        return False, None, "empty phone"
    if not wa_conf.is_phone_allowed(phone):
        logger.info("WA template: TEST_MODE skip phone=%s", phone)
        return False, None, "test_mode_skip"

    params = []
    if body_params:
        # Параметры шаблона Meta не допускают переносов/табов/4+ пробелов —
        # схлопываем любой whitespace в одиночный пробел.
        clean = [re.sub(r"\s+", " ", str(p)).strip() for p in body_params]
        params.append({
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in clean],
        })

    payload = {
        "phone": phone,
        "namespace": namespace or wa_conf.NAMESPACE,
        "template": template_name,
        "language": {"policy": "deterministic", "code": language_code or "ru"},
        "params": params,
    }
    ok, data, err = _post("sendTemplate", payload, timeout=45)
    return ok, (data.get("id") if ok else None), err


def create_whatsapp_template(
    name: str,
    body_text: str,
    category: str,
    language_code: str = "ru",
    body_example: Optional[list] = None,
    allow_category_change: bool = True,
) -> Tuple[bool, dict, Optional[str]]:
    """Создать (отправить на модерацию Meta) WABA-шаблон через ``addTemplate``.

    ``body_text`` — текст с плейсхолдерами {{1}}, {{2}}…
    ``body_example`` — список примеров значений переменных (требование Meta,
    если в тексте есть {{N}}).
    ``category`` — UTILITY / MARKETING / AUTHENTICATION.

    Возвращает ``(ok, data, err)`` — в ``data`` обычно ``id`` нового шаблона.
    """
    component = {"type": "BODY", "text": body_text}
    if body_example:
        component["example"] = {"body_text": [[str(x) for x in body_example]]}
    payload = {
        "name": name,
        "allow_category_change": allow_category_change,
        "category": category,
        "language": language_code or "ru",
        "components": [component],
    }
    return _post("addTemplate", payload, timeout=45)


def list_whatsapp_templates() -> Tuple[bool, list, Optional[str]]:
    """Список всех WABA-шаблонов инстанса (для синка статусов модерации)."""
    if not wa_conf.is_configured():
        return False, [], "1msg не настроен"
    url = f"{wa_conf.API_BASE}/{wa_conf.INSTANCE_ID}/templates?token={wa_conf.API_TOKEN}"
    try:
        r = requests.get(url, timeout=45)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("WA list_templates failed: %s", e)
        return False, [], str(e)
    return True, (data.get("templates") or []), None


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
