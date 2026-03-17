# apps/maxchat/sender.py
import httpx
from django.conf import settings

MAX_API_BASE_URL = settings.MAX_API_BASE_URL.rstrip("/")
MAX_ACCESS_TOKEN = settings.MAX_BOT_TOKEN


async def send_max_message(*, user_id: str, text: str):
    """
    Отправка простого текстового сообщения в MAX Bot API.
    """
    url = f"{MAX_API_BASE_URL}/messages"
    headers = {
        "Authorization": MAX_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "user_id": user_id,
        "text": text,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return {
            "success": True,
            "id": str(data.get("id") or data.get("message_id", "")),
            "raw": data,
        }
