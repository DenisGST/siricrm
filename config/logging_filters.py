"""Логгинг-фильтры проекта.

NoDisallowedHostFilter — отсекает traceback'и от сканеров, стучащихся
с фейковым Host. Django на такой запрос raise'ит DisallowedHost, отвечает
клиенту 400, но порождает по два лог-события:

1. `django.security.DisallowedHost.error("Invalid HTTP_HOST header ...")` —
   короткая строка без traceback. Глушится ноль-хендлером в LOGGING loggers.
2. `django.request.error("Bad Request ...", exc_info=exc)` из log_response()
   в django/core/handlers/base.py — вот тот длинный traceback, что валится
   в файл-лог по цепочке пропагации до parent `django` → handlers ['console','file'].

Отдельно глушить `django.request` нельзя — он пишет и полезные warnings
о реальных 4xx/5xx. Поэтому фильтр на handler'ах:
пропускает всё, кроме событий с exc_info-DisallowedHost.
"""
import logging

from django.core.exceptions import DisallowedHost


class NoDisallowedHostFilter(logging.Filter):
    """Дропает LogRecord, если исключение — DisallowedHost."""

    def filter(self, record):
        exc_info = getattr(record, "exc_info", None)
        if exc_info and exc_info[0] and issubclass(exc_info[0], DisallowedHost):
            return False
        return True
