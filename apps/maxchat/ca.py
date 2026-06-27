"""CA-бандл для запросов к MAX.

С 19.07.2026 MAX переводит API на ``platform-api2.max.ru``, чей TLS-сертификат
выдан CA Минцифры («Russian Trusted Root CA») — его НЕТ в дефолтном bundle
``certifi``, поэтому ``requests`` падает с ``CERTIFICATE_VERIFY_FAILED``.

Собираем объединённый bundle = ``certifi`` (обычные мировые CA) + Russian
Trusted CA (Минцифры). Подходит и для platform-api2 (рос. CA), и для CDN
``i.oneme.ru``/``fd.oneme.ru`` (обычные CA) — суперсет, ничего не ломает.
"""
import functools
import os

import certifi
from django.conf import settings


@functools.lru_cache(maxsize=1)
def max_ca_bundle() -> str:
    """Путь к объединённому CA-бандлу (certifi + Russian Trusted CA).

    Пишем во временный файл один раз за процесс (кэш). Если рос. CA в репо нет —
    откатываемся на стандартный ``certifi`` (старый platform-api.max.ru работает
    и без него)."""
    russian = getattr(settings, "MAX_CA_BUNDLE", "")
    if not russian or not os.path.exists(russian):
        return certifi.where()

    out = os.path.join("/tmp", "max_ca_bundle.pem")
    try:
        with open(out, "wb") as dst:
            for src in (certifi.where(), russian):
                with open(src, "rb") as f:
                    dst.write(f.read())
                    dst.write(b"\n")
        return out
    except OSError:
        # /tmp недоступен — отдаём хотя бы рос. CA (для platform-api2)
        return russian
