"""Отправка сообщений сотрудникам через Telegram Bot API (@Sirius_system_bot).

Отдельно от `telegram_sender.py`: тот шлёт от имени userbot'а (Telethon,
аккаунт компании) КЛИЕНТАМ, а этот — служебные уведомления СОТРУДНИКАМ
от бота (тот же бот, что принимает лиды и коды привязки).

🛑 Бот не может написать первым: сотрудник обязан один раз нажать Start
(или отправить код привязки) — иначе Telegram отвечает «chat not found».
"""
from __future__ import annotations

import html
import logging

import requests
from django.conf import settings

logger = logging.getLogger("telegram_bot")

API_TIMEOUT = 20
# Telegram режет сообщения длиннее 4096 символов — держим запас.
MAX_TEXT_LEN = 3800


def bot_token() -> str:
    return (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()


def esc(s: str) -> str:
    """Экранировать текст для parse_mode=HTML (в т.ч. подпись ссылки)."""
    return html.escape(s or "", quote=False)


def send_bot_message(
    chat_id, text: str, *, parse_mode: str = "HTML",
    disable_web_page_preview: bool = True,
) -> tuple[bool, str | None, str | None]:
    """Отправить сообщение сотруднику. → (ok, message_id, error).

    Сигнатура зеркалит `apps.maxchat.sender.send_max_message`, чтобы
    вызывающий код обоих каналов выглядел одинаково.
    """
    token = bot_token()
    if not token:
        return False, None, "TELEGRAM_BOT_TOKEN не задан"

    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload, timeout=API_TIMEOUT,
        )
    except requests.RequestException as e:
        return False, None, str(e)

    try:
        data = r.json()
    except ValueError:
        return False, None, f"http_{r.status_code}: {r.text[:200]}"

    if not data.get("ok"):
        return False, None, data.get("description") or f"http_{r.status_code}"
    return True, str((data.get("result") or {}).get("message_id") or ""), None
