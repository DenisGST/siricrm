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
