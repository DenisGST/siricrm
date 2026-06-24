"""Глобальный 12-часовой cooldown после капчи от kad.arbitr.ru.

Когда kad показывает капчу — её надо решать человеку в браузере, а наша
сессия в этот момент сожжена. Любые попытки парсить дальше:
  * капчу не пройдут (kad возвращает её на ВСЕ запросы с этого IP),
  * только нагенерируют ещё больше алёртов в MAX (флуд).

Поэтому при первой капче активируем глобальный cooldown — все задачи
(kad_monitor_pending, kad_monitor_case, kad_monitor_one_case,
management-команда parse_all_monitoring) на 12 часов отказываются
запускаться. Алёрт о капче улетает в MAX ровно один раз, с указанием
времени возобновления.

Снять cooldown вручную (решил капчу раньше):
  python manage.py arbitr_clear_cooldown
"""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

COOLDOWN_KEY = "arbitr:captcha_cooldown_until"
COOLDOWN_SECONDS = 12 * 3600


def is_active() -> bool:
    return cache.get(COOLDOWN_KEY) is not None


def until():
    """Возвращает aware datetime до которого cooldown активен, или None."""
    iso = cache.get(COOLDOWN_KEY)
    if not iso:
        return None
    return parse_datetime(iso)


def activate() -> bool:
    """Активирует cooldown на COOLDOWN_SECONDS. Возвращает:
      True  — только что активировали (надо слать алёрт);
      False — уже был активен (алёрт уже улетел, молчим).
    """
    if cache.get(COOLDOWN_KEY) is not None:
        return False
    end = timezone.now() + timedelta(seconds=COOLDOWN_SECONDS)
    cache.set(COOLDOWN_KEY, end.isoformat(), timeout=COOLDOWN_SECONDS)
    return True


def clear() -> None:
    cache.delete(COOLDOWN_KEY)
