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
    content_type: str = None,
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
    import mimetypes
    if not content_type:
        content_type, _ = mimetypes.guess_type(filename)
        content_type = content_type or "application/octet-stream"

    try:
        r2 = requests.post(
            upload_url,
            files={"data": (filename, file_bytes, content_type)},  # ← передаём content_type
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

def _wait_attachment_ready(
    access_token: str,
    chat_id: str,
    payload: dict,
    max_attempts: int = 8,
    delay: float = 3.0,
) -> Tuple[bool, Optional[str], Optional[str]]:
    headers = {
        "Authorization": access_token,
        "Content-Type": "application/json",
    }
    params = {"user_id": chat_id}

    for attempt in range(1, max_attempts + 1):
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

        logger.info("MAX send attempt=%d status=%s body=%s", attempt, resp.status_code, resp.text[:300])

        if resp.status_code == 400:
            try:
                err_data = resp.json()
            except Exception:
                err_data = {}
            if err_data.get("code") == "attachment.not.ready":
                logger.warning("MAX attachment not ready, attempt %d/%d, waiting %.1fs", attempt, max_attempts, delay)
                time.sleep(delay)
                continue
            return False, None, f"HTTP 400: {resp.text}"

        if resp.status_code >= 400:
            return False, None, f"HTTP {resp.status_code}: {resp.text}"

        try:
            data = resp.json()
        except Exception:
            return False, None, "Invalid JSON from MAX"

        message = data.get("message") or data
        message_id = message.get("id") or message.get("mid") or data.get("id")
        return True, str(message_id) if message_id else "unknown", None

    return False, None, f"attachment.not.ready after {max_attempts} attempts"


def send_max_message(
    *,
    access_token: str,
    chat_id: str,
    text: str,
    file_bytes: bytes = None,
    filename: str = None,
    message_type: str = "text",
    content_type: str = None,
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
                access_token, file_bytes, filename or "file", message_type, content_type
            )
            if not ok:
                return False, None, f"Upload failed: {err}"

            upload_type = _get_upload_type(message_type)
            payload["attachments"] = [{"type": upload_type, "payload": att_payload}]

            # Polling вместо фиксированного sleep
            return _wait_attachment_ready(access_token, chat_id, payload)

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
