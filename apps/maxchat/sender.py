# apps/maxchat/sender.py
import logging
import time
from typing import Optional, Tuple

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_API_BASE_URL = "https://platform-api.max.ru"


def _get_upload_type(message_type: str) -> str:
    """Маппинг message_type → тип загрузки MAX API."""
    return {
        "image": "image",
        "video": "video",
        "audio": "audio",
        "voice": "audio",
        "document": "file",
    }.get(message_type, "file")


def _upload_file_to_max(
    access_token: str,
    file_bytes: bytes,
    filename: str,
    message_type: str,
) -> Tuple[bool, Optional[dict], Optional[str]]:
    upload_type = _get_upload_type(message_type)
    headers_auth = {"Authorization": access_token}

    # Шаг 1: получаем URL для загрузки
    try:
        r = requests.post(
            f"{MAX_API_BASE_URL}/uploads",
            params={"type": upload_type},
            headers=headers_auth,
            timeout=10,
        )
        r.raise_for_status()
        upload_data = r.json()
    except Exception as e:
        logger.exception("MAX upload: failed to get upload URL: %s", e)
        return False, None, str(e)

    upload_url = upload_data.get("url")
    pre_token = upload_data.get("token")  # для video/audio

    if not upload_url:
        return False, None, f"No upload URL in response: {upload_data}"

        # Шаг 2: загружаем файл — БЕЗ Authorization заголовка!
    try:
        r2 = requests.post(
            upload_url,
            files={"data": (filename, file_bytes)},  # без headers
            timeout=60,
        )
        r2.raise_for_status()
        upload_result = r2.json()
    except Exception as e:
        logger.exception("MAX upload: failed to upload file: %s", e)
        return False, None, str(e)


    logger.info("MAX upload result type=%s: %s", upload_type, upload_result)

    # Токен может быть в разных местах в зависимости от типа
    token = upload_result.get("token")

    if not token:
        # image → {"photos": {"<hash>": {"token": "..."}}}
        # file  → {"files":  {"<hash>": {"token": "..."}}}
        # video → {"videos": {"<hash>": {"token": "..."}}}
        # audio → {"audios": {"<hash>": {"token": "..."}}}
        for key in ("photos", "files", "videos", "audios"):
            nested = upload_result.get(key)
            if nested and isinstance(nested, dict):
                first = next(iter(nested.values()))
                token = first.get("token")
                if token:
                    break

    # Для video/audio токен мог прийти на шаге 1
    if not token:
        token = pre_token

    if not token:
        return False, None, f"No token in upload result: {upload_result}"

    return True, {"token": token}, None



def send_max_message(
    *,
    access_token: str,
    chat_id: str,
    text: str,
    file_bytes: bytes = None,
    filename: str = None,
    message_type: str = "text",
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Отправка сообщения в MAX (текст + медиа).
    Возвращает (success, message_id, error_text).
    """
    headers = {
        "Authorization": access_token,
        "Content-Type": "application/json",
    }
    params = {"user_id": chat_id}
    payload = {}

    if text:
        payload["text"] = text

    # Если есть файл — сначала загружаем
    if file_bytes and message_type != "text":
        ok, att_payload, err = _upload_file_to_max(
            access_token, file_bytes, filename or "file", message_type
        )
        if not ok:
            return False, None, f"Upload failed: {err}"

        upload_type = _get_upload_type(message_type)

        payload["attachments"] = [
            {
                "type": upload_type,
                "payload": att_payload,
            }
        ]

        # Небольшая пауза чтобы сервер обработал файл
        if message_type in ("video", "audio", "voice"):
            time.sleep(2)

    try:
        resp = requests.post(
            f"{MAX_API_BASE_URL}/messages",
            params=params,
            json=payload,
            headers=headers,
            timeout=15,
        )
    except Exception as e:
        logger.exception("MAX send_message network error: %s", e)
        return False, None, str(e)

    logger.info("MAX send_message status=%s body=%s", resp.status_code, resp.text[:300])

    if resp.status_code >= 400:
        logger.warning("MAX send_message HTTP %s: %s", resp.status_code, resp.text)
        return False, None, f"HTTP {resp.status_code}: {resp.text}"

    try:
        data = resp.json()
    except Exception:
        return False, None, "Invalid JSON from MAX"

    message = data.get("message") or data
    message_id = message.get("id") or message.get("mid") or data.get("id")

    if not message_id:
        if resp.status_code < 400:
            return True, "unknown", None
        return False, None, f"No message id in response: {data}"

    return True, str(message_id), None
