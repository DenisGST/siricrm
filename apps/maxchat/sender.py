# apps/maxchat/sender.py
import logging
from typing import Optional, Tuple

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_API_BASE_URL = "https://platform-api.max.ru"  # базовый урл API [web:102]


def send_max_message(*, access_token: str, chat_id: str, text: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Отправка текстового сообщения в MAX.
    Возвращает (success, message_id, error_text).
    """
    url = f"{MAX_API_BASE_URL}/messages"  # метод отправки сообщений [web:102]

    headers = {
        "Authorization": access_token,
        "Content-Type": "application/json",
    }

    payload = {
        "chat_id": chat_id,
        "message": {
            "text": text,
        },
    }  # структура тела по доке Message [web:251]

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        logger.exception("MAX send_message network error: %s", e)
        return False, None, str(e)

    if resp.status_code >= 400:
        logger.warning("MAX send_message HTTP %s: %s", resp.status_code, resp.text)
        return False, None, f"HTTP {resp.status_code}: {resp.text}"

    try:
        data = resp.json()
    except Exception:
        logger.warning("MAX send_message: invalid JSON: %s", resp.text)
        return False, None, "Invalid JSON from MAX"

    # по объекту Message id обычно в корне или в поле id [web:251]
    message_id = data.get("id") or data.get("message_id")
    if not message_id:
        logger.warning("MAX send_message: no message id in response: %s", data)
        return False, None, "No message id in response"

    return True, str(message_id), None
