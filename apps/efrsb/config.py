"""Конфиг интеграции ЕФРСБ — обёртка над settings (секреты только из env)."""
from __future__ import annotations

from django.conf import settings

# Лимит ЕФРСБ: не более 8 запросов/сек с одного IP. Держим минимальный интервал
# между вызовами с запасом (1 воркер) — см. client._throttle.
RATE_LIMIT_PER_SEC = 8
MIN_INTERVAL_SEC = 0.15
HTTP_TIMEOUT = 30
JWT_TTL_SEC = int(7.5 * 3600)  # токен живёт 8ч — кэшируем 7.5ч с запасом


def contour() -> str:
    return (getattr(settings, "EFRSB_CONTOUR", "demo") or "demo").lower()


def base_url() -> str:
    if contour() == "prod":
        return settings.EFRSB_PROD_BASE_URL.rstrip("/")
    return settings.EFRSB_DEMO_BASE_URL.rstrip("/")


def credentials() -> tuple[str, str]:
    return settings.EFRSB_LOGIN, settings.EFRSB_PASSWORD


def is_configured() -> bool:
    login, password = credentials()
    return bool(getattr(settings, "EFRSB_ENABLED", False) and login and password)


def monitor_enabled() -> bool:
    return bool(is_configured() and getattr(settings, "EFRSB_MONITOR_ENABLED", False))


def sync_interval_hours() -> int:
    return int(getattr(settings, "EFRSB_SYNC_INTERVAL_HOURS", 4))


def search_retry_hours() -> int:
    return int(getattr(settings, "EFRSB_SEARCH_RETRY_HOURS", 24))


def download_files_default() -> bool:
    return bool(getattr(settings, "EFRSB_DOWNLOAD_FILES", False))
