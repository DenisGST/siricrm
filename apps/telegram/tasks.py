"""Celery-задачи для интеграции с Telegram Bot API.

`poll_telegram_leads` дёргается из Celery beat каждые ~10 секунд:
вызывает getUpdates с long-polling timeout=20, прогоняет каждое
сообщение через парсер `_parse_lead` и создаёт лиды в CRM.

Используется как альтернатива webhook'у — там, где Telegram не
может достучаться до сервера (наш случай: split-tunnel WireGuard
заворачивает ответный SYN-ACK обратно в туннель).
"""
import logging

import requests
from celery import shared_task
from django.core.cache import cache

from .leads_bot import (
    BOT_TOKEN, LEADS_CHANNEL_ID, _parse_lead, create_lead_from_parsed,
)

logger = logging.getLogger("telegram_leads")

OFFSET_CACHE_KEY = "telegram_leads:update_offset"
POLL_LOCK_KEY = "telegram_leads:poll_lock"
POLL_TIMEOUT = 20  # секунд long-polling


@shared_task(name="telegram.poll_telegram_leads", time_limit=POLL_TIMEOUT + 15)
def poll_telegram_leads():
    """Один цикл getUpdates → парс → создание лидов. Возвращает кол-во
    созданных/повторных лидов. Параллельные вызовы getUpdates Telegram
    отбивает 409 Conflict — поэтому держим лок на время цикла."""
    if not BOT_TOKEN:
        return {"skipped": "no_bot_token"}

    # SETNX-лок через cache.add — отсекает соседние beat-тики, пока long
    # polling висит. Таймаут с запасом, чтобы лок снялся, даже если воркер упал.
    if not cache.add(POLL_LOCK_KEY, "1", timeout=POLL_TIMEOUT + 10):
        return {"skipped": "locked"}

    try:
        return _poll_once()
    finally:
        cache.delete(POLL_LOCK_KEY)


def _poll_once():
    offset = cache.get(OFFSET_CACHE_KEY)
    params = {
        "timeout": POLL_TIMEOUT,
        "allowed_updates": '["channel_post","edited_channel_post","message"]',
    }
    if offset is not None:
        params["offset"] = offset

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params=params, timeout=POLL_TIMEOUT + 10)
    except requests.RequestException as e:
        logger.warning("telegram-leads polling: запрос упал: %s", e)
        return {"error": str(e)}

    if r.status_code != 200:
        logger.warning("telegram-leads polling: %s %s", r.status_code, r.text[:300])
        return {"error": f"http_{r.status_code}"}

    data = r.json()
    if not data.get("ok"):
        return {"error": data.get("description")}

    updates = data.get("result") or []
    leads = 0
    last_id = None
    for upd in updates:
        last_id = upd.get("update_id")
        msg = (
            upd.get("channel_post")
            or upd.get("edited_channel_post")
            or upd.get("message")
        )
        if not msg:
            continue
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if LEADS_CHANNEL_ID and chat_id != LEADS_CHANNEL_ID:
            logger.info("telegram-leads polling: пропущен chat_id=%s", chat_id)
            continue
        text = msg.get("text") or msg.get("caption") or ""
        parsed = _parse_lead(text)
        if not parsed:
            continue
        try:
            create_lead_from_parsed(parsed)
            leads += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("telegram-leads polling: ошибка создания лида: %s", e)

    if last_id is not None:
        # offset = last + 1 — Telegram перестанет отдавать обработанные.
        cache.set(OFFSET_CACHE_KEY, last_id + 1, timeout=7 * 24 * 3600)

    return {"updates": len(updates), "leads": leads}
