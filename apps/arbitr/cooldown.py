"""Per-IP 12-часовой cooldown после капчи от kad.arbitr.ru.

Когда kad показывает капчу — она привязана к **IP** который её получил.
Капча решается человеком в браузере (с того же IP), но мы это сделать
не можем. Поэтому: блокируем парсинг на этом IP на 12ч, остальные IP
продолжают работать.

Архитектура (3 параллельных runner'а с per-IP SNAT — см. tasks.py
и ops/arbitr-snat-rotate.sh):
  * Каждый runner парсит через свой outbound IP.
  * При капче handle_captcha(case, page_url, ip) активирует cooldown
    ИМЕННО для этого IP и шлёт ОДИН алёрт в MAX.
  * Каждый тик `_kad_smart_one` проверяет `is_active(runner_ip)` —
    если этот runner на отдыхающем IP, тик пропускается. Остальные
    runner'ы на здоровых IP — продолжают.
  * 12ч истекают → ключ Redis TTL сам удалится → парсер возобновится.

Снять cooldown вручную:
  python manage.py arbitr_clear_cooldown                # все IP
  python manage.py arbitr_clear_cooldown --ip 1.2.3.4   # один
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

KEY_PREFIX = "arbitr:captcha_cooldown_until:"
COOLDOWN_SECONDS = 12 * 3600
DJANGO_CACHE_VERSION_PREFIX = ":1:"


def _key(ip: str) -> str:
    return KEY_PREFIX + (ip or "unknown")


def is_active(ip: str) -> bool:
    return cache.get(_key(ip)) is not None


def until(ip: str):
    """Aware datetime, до которого cooldown активен на этом IP, или None."""
    iso = cache.get(_key(ip))
    if not iso:
        return None
    return parse_datetime(iso)


def activate(ip: str) -> bool:
    """Активирует cooldown для IP на COOLDOWN_SECONDS. Возвращает:
      True  — только что активировали (надо слать алёрт);
      False — уже был активен (молчим).
    """
    if cache.get(_key(ip)) is not None:
        return False
    end = timezone.now() + timedelta(seconds=COOLDOWN_SECONDS)
    cache.set(_key(ip), end.isoformat(), timeout=COOLDOWN_SECONDS)
    return True


def clear(ip: str | None = None) -> int:
    """Снимает cooldown. Если ip задан — только его, иначе все.
    Возвращает количество снятых записей.
    """
    if ip:
        if cache.get(_key(ip)) is not None:
            cache.delete(_key(ip))
            return 1
        return 0
    # Очистка всех — scan keys через redis-py (django cache не умеет).
    try:
        import redis  # noqa: WPS433
        r = redis.Redis.from_url(settings.REDIS_URL)
        pattern = f"{DJANGO_CACHE_VERSION_PREFIX}{KEY_PREFIX}*"
        keys = list(r.scan_iter(pattern))
        if keys:
            r.delete(*keys)
        return len(keys)
    except Exception:
        return 0


def all_active() -> dict:
    """{ip: aware_until_datetime} для всех активных cooldown'ов."""
    out: dict[str, "timezone.datetime"] = {}
    try:
        import redis  # noqa: WPS433
        r = redis.Redis.from_url(settings.REDIS_URL)
        pattern = f"{DJANGO_CACHE_VERSION_PREFIX}{KEY_PREFIX}*"
        for k in r.scan_iter(pattern):
            key_str = k.decode("utf-8")
            ip = key_str.split(KEY_PREFIX, 1)[-1]
            val = r.get(k)
            if val:
                dt = parse_datetime(val.decode("utf-8"))
                if dt:
                    out[ip] = dt
    except Exception:
        pass
    return out
