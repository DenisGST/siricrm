"""Фоновые задачи ядра.

monitor_health — внешний мониторинг доступности другого окружения:
GET <HEALTH_MONITOR_TARGET_URL>/health/ раз в минуту (beat), при N неудачах
подряд шлёт алёрт в MAX и Telegram, при восстановлении — «ОК».

Кросс-серверная схема: dev мониторит прод, прод мониторит dev. Так алёрт
придёт, даже если у целевого сервера ляжет весь стек (web/celery/redis), —
именно этого не хватило при инциденте 09.06.2026 (web завис на 6 ч). Задача
крутится в Celery МОНИТОРЯЩЕГО сервера и использует его собственный
redis/боты, не завися от состояния целевого.

Включается только там, где задан HEALTH_MONITOR_TARGET_URL (иначе no-op).
"""
import logging

import requests
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("core.monitor")

_FAILS_KEY = "health_monitor:fails"
_DOWN_KEY = "health_monitor:down"
_STATE_TTL = 86400  # сутки


def _send_telegram_alert(text: str) -> bool:
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(settings, "HEALTH_ALERT_TELEGRAM_CHAT_ID", "") or "").strip()
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            logger.error("health alert TG send failed: %s %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception:
        logger.exception("health alert TG send error")
        return False


def _send_max_alert(text: str) -> bool:
    token = (getattr(settings, "MAX_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(settings, "HEALTH_ALERT_MAX_CHAT_ID", "") or "").strip()
    if not token or not chat_id:
        return False
    try:
        from apps.maxchat.sender import send_max_message
        ok, _mid, err = send_max_message(access_token=token, chat_id=chat_id, text=text)
        if not ok:
            logger.error("health alert MAX send failed: %s", err)
        return ok
    except Exception:
        logger.exception("health alert MAX send error")
        return False


def _alert(text: str):
    sent_max = _send_max_alert(text)
    sent_tg = _send_telegram_alert(text)
    logger.info("health alert отправлен: MAX=%s TG=%s", sent_max, sent_tg)
    if not sent_max and not sent_tg:
        logger.error("health alert НЕ доставлен ни в MAX, ни в Telegram — проверьте "
                     "HEALTH_ALERT_MAX_CHAT_ID / HEALTH_ALERT_TELEGRAM_CHAT_ID и токены")


@shared_task
def monitor_health():
    url = (getattr(settings, "HEALTH_MONITOR_TARGET_URL", "") or "").strip()
    if not url:
        return  # мониторинг на этом сервере выключен
    label = (getattr(settings, "HEALTH_MONITOR_LABEL", "") or url).strip()
    threshold = int(getattr(settings, "HEALTH_MONITOR_FAIL_THRESHOLD", 2) or 2)
    host = (getattr(settings, "HEALTH_MONITOR_HOST", "") or "").strip()
    headers = {"Host": host} if host else {}

    healthy = False
    detail = ""
    try:
        r = requests.get(url, timeout=10, allow_redirects=True, headers=headers)
        detail = f"HTTP {r.status_code}"
        healthy = r.status_code == 200
        if not healthy:
            try:
                detail += f" {r.json()}"
            except Exception:
                pass
    except Exception as e:
        detail = f"нет ответа ({e.__class__.__name__})"

    down = bool(cache.get(_DOWN_KEY))

    if healthy:
        cache.set(_FAILS_KEY, 0, _STATE_TTL)
        if down:
            cache.set(_DOWN_KEY, False, _STATE_TTL)
            _alert(f"✅ ВОССТАНОВЛЕНО: {label} снова отвечает ({detail}).")
        return

    fails = int(cache.get(_FAILS_KEY) or 0) + 1
    cache.set(_FAILS_KEY, fails, _STATE_TTL)
    logger.warning("health monitor: %s недоступен (%s), подряд %d/%d", label, detail, fails, threshold)
    if fails >= threshold and not down:
        cache.set(_DOWN_KEY, True, _STATE_TTL)
        now = timezone.localtime().strftime("%d.%m.%Y %H:%M:%S")
        _alert(
            f"🛑 НЕДОСТУПЕН: {label}\n"
            f"Проверка: {detail}\n"
            f"Неудач подряд: {fails}\n"
            f"Время: {now} (МСК)\n"
            f"URL: {url}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram-бот мониторинга: кнопки «Статус прод» / «Статистика».
# Поллер живёт на dev (MONITOR_BOT_POLL=true), отвечает только авторизованному
# чату (HEALTH_ALERT_TELEGRAM_CHAT_ID). Данные прода берёт через прод
# DevOps-агент (status / daily_stats) — работает даже если прод-web завис.
# ─────────────────────────────────────────────────────────────────────────────
import json as _json
import time as _time

_MON_OFFSET_KEY = "monitor_bot:offset"
_MON_LOCK_KEY = "monitor_bot:lock"
_MON_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "🖥 Статус прода", "callback_data": "prod_status"}],
        [{"text": "📊 Статистика за сутки", "callback_data": "prod_stats"}],
    ]
}


def _tg_api(method: str, payload: dict):
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    if not token:
        return None
    try:
        return requests.post(
            f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20,
        )
    except Exception:
        logger.exception("monitor bot: TG %s failed", method)
        return None


def _call_prod_agent(action: str, params: dict = None, timeout: int = 45):
    """POST job в прод DevOps-агент и ждём результат. → (ok, output, error)."""
    base = (getattr(settings, "PROD_AGENT_URL", "") or "").rstrip("/")
    token = (getattr(settings, "DEVOPS_AGENT_TOKEN_PROD", "") or "").strip()
    if not base or not token:
        return False, None, "PROD_AGENT_URL / DEVOPS_AGENT_TOKEN_PROD не заданы"
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.post(f"{base}/devops/agent/jobs/", headers=hdr,
                          json={"action_type": action, "params": params or {}}, timeout=20)
    except requests.RequestException as e:
        return False, None, f"прод недоступен ({e.__class__.__name__})"
    if r.status_code >= 400:
        return False, None, f"HTTP {r.status_code}"
    job_id = (r.json() or {}).get("id")
    if not job_id:
        return False, None, "агент не вернул id задачи"
    for _ in range(max(1, timeout // 2)):
        _time.sleep(2)
        try:
            jr = requests.get(f"{base}/devops/agent/jobs/{job_id}/", headers=hdr, timeout=15).json()
        except requests.RequestException:
            continue
        if jr.get("status") == "done":
            return True, (jr.get("output") or "(пусто)"), None
        if jr.get("status") == "failed":
            return False, None, (jr.get("output") or "задача упала")
    return False, None, "таймаут ожидания агента"


def _send_prod_report(chat_id: str, action: str, wait_text: str):
    _tg_api("sendMessage", {"chat_id": chat_id, "text": wait_text})
    ok, out, err = _call_prod_agent(action)
    if ok:
        _tg_api("sendMessage", {"chat_id": chat_id, "text": f"<pre>{out[:3900]}</pre>",
                                "parse_mode": "HTML"})
    else:
        _tg_api("sendMessage", {"chat_id": chat_id, "text": f"❌ Не удалось получить данные: {err}"})


def _handle_monitor_update(upd: dict, allowed: str):
    cb = upd.get("callback_query")
    if cb:
        _tg_api("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": "Собираю…"})
        chat_id = str((cb.get("message") or {}).get("chat", {}).get("id")
                      or (cb.get("from") or {}).get("id") or "")
        if allowed and chat_id != allowed:
            return
        data = cb.get("data")
        if data == "prod_status":
            _send_prod_report(chat_id, "status", "🖥 Собираю статус прода…")
        elif data == "prod_stats":
            _send_prod_report(chat_id, "daily_stats", "📊 Считаю статистику за сутки…")
        return

    msg = upd.get("message")
    if msg:
        chat_id = str(msg.get("chat", {}).get("id") or "")
        if allowed and chat_id != allowed:
            return
        _tg_api("sendMessage", {"chat_id": chat_id,
                                "text": "Мониторинг прод-сервера — выберите отчёт:",
                                "reply_markup": _MON_KEYBOARD})


def _poll_monitor_once(token: str, allowed: str):
    params = {"timeout": 20, "allowed_updates": _json.dumps(["message", "callback_query"])}
    offset = cache.get(_MON_OFFSET_KEY)
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                            params=params, timeout=30)
        updates = (resp.json() or {}).get("result", [])
    except Exception:
        logger.exception("monitor bot: getUpdates failed")
        return
    for upd in updates:
        cache.set(_MON_OFFSET_KEY, upd["update_id"] + 1, 3600)
        try:
            _handle_monitor_update(upd, allowed)
        except Exception:
            logger.exception("monitor bot: handle update failed")


@shared_task
def poll_monitor_bot():
    if not getattr(settings, "MONITOR_BOT_POLL", False):
        return
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    allowed = str(getattr(settings, "HEALTH_ALERT_TELEGRAM_CHAT_ID", "") or "").strip()
    if not token:
        return
    # SETNX-лок: один long-poll за раз (иначе параллельные getUpdates → 409).
    if not cache.add(_MON_LOCK_KEY, "1", 28):
        return
    try:
        _poll_monitor_once(token, allowed)
    finally:
        cache.delete(_MON_LOCK_KEY)
