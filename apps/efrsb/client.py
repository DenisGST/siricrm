"""HTTP-клиент read-API ЕФРСБ (получение сведений).

Спецификация: «Сервис получения сведений из ЕФРСБ» v1.0.0. Только чтение.
  • auth: POST /v1/auth {login,password} → {jwt}; токен 8ч, кэшируем в Redis 7.5ч.
  • Authorization: Bearer <jwt> на каждом вызове; HTTPS only; лимит 8 req/s/IP.
  • даты-фильтры: gte:/lte:/gt:/lt:/eq: + ISO; диапазон ≤31 дня без number/guid/bankruptGUID.

401 → авто-релогин + 1 повтор. 429 → EfrsbRateLimited (таска делает backoff).
5xx → ретрай ×3 с экспон. задержкой. Прочее → EfrsbError.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Iterator, Optional

import requests
from django.core.cache import cache

from . import config

log = logging.getLogger(__name__)

_JWT_CACHE_KEY = "efrsb:jwt"
_THROTTLE_KEY = "efrsb:last_call_ts"


class EfrsbError(RuntimeError):
    pass


class EfrsbNotConfigured(EfrsbError):
    pass


class EfrsbAuthError(EfrsbError):
    pass


class EfrsbRateLimited(EfrsbError):
    pass


# ── auth / token ────────────────────────────────────────────────────────────

def get_jwt(*, force: bool = False) -> str:
    if not config.is_configured():
        raise EfrsbNotConfigured("ЕФРСБ не настроен (нет кредов/EFRSB_ENABLED).")
    if not force:
        cached = cache.get(_JWT_CACHE_KEY)
        if cached:
            return cached
    login, password = config.credentials()
    url = f"{config.base_url()}/v1/auth"
    try:
        r = requests.post(url, json={"login": login, "password": password},
                          timeout=config.HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise EfrsbError(f"auth: сеть недоступна: {e}") from e
    if r.status_code != 200:
        raise EfrsbAuthError(f"auth: HTTP {r.status_code}: {r.text[:300]}")
    token = (r.json() or {}).get("jwt")
    if not token:
        raise EfrsbAuthError("auth: пустой jwt в ответе")
    cache.set(_JWT_CACHE_KEY, token, timeout=config.JWT_TTL_SEC)
    return token


def _throttle():
    """Грубый распределённый throttle: держим ≥MIN_INTERVAL между вызовами."""
    last = cache.get(_THROTTLE_KEY)
    now = time.monotonic()
    if last is not None:
        wait = config.MIN_INTERVAL_SEC - (now - last)
        if wait > 0:
            time.sleep(wait)
    cache.set(_THROTTLE_KEY, time.monotonic(), timeout=5)


def _request(method: str, path: str, *, params=None, stream=False,
             _retry_auth: bool = True, _retries_5xx: int = 3) -> requests.Response:
    url = f"{config.base_url()}{path}"
    _throttle()
    headers = {"Authorization": f"Bearer {get_jwt()}", "Accept": "application/json"}
    try:
        r = requests.request(method, url, headers=headers, params=params,
                             stream=stream, timeout=config.HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise EfrsbError(f"{method} {path}: сеть недоступна: {e}") from e

    if r.status_code == 401 and _retry_auth:
        get_jwt(force=True)
        return _request(method, path, params=params, stream=stream,
                        _retry_auth=False, _retries_5xx=_retries_5xx)
    if r.status_code == 429:
        raise EfrsbRateLimited(f"{method} {path}: 429 Too Many Requests")
    if r.status_code >= 500 and _retries_5xx > 0:
        time.sleep(2 ** (3 - _retries_5xx))  # 1s, 2s, 4s
        return _request(method, path, params=params, stream=stream,
                        _retry_auth=_retry_auth, _retries_5xx=_retries_5xx - 1)
    if r.status_code >= 400:
        raise EfrsbError(f"{method} {path}: HTTP {r.status_code}: {r.text[:300]}")
    return r


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_dt(value, op: str) -> Optional[str]:
    """Формат date-фильтра: '<op>:гггг-мм-ддTчч:мм:сс'."""
    if value is None:
        return None
    if isinstance(value, str):
        return value  # уже сформирован вызывающим
    if isinstance(value, datetime):
        return f"{op}:{value:%Y-%m-%dT%H:%M:%S}"
    if isinstance(value, date):
        return f"{op}:{value:%Y-%m-%d}T00:00:00"
    return None


def _bool(v):
    if v is None:
        return None
    return "true" if v else "false"


def _clean(params: dict) -> dict:
    return {k: v for k, v in params.items() if v not in (None, "")}


# ── messages ───────────────────────────────────────────────────────────────

def get_messages(*, bankrupt_guid=None, date_begin=None, date_end=None, type=None,
                 number=None, guid=None, is_annulled=None, is_locked=None,
                 include_content=False, sort="DatePublish:asc",
                 limit=500, offset=0) -> dict:
    params = _clean({
        "bankruptGUID": bankrupt_guid,
        "datePublishBegin": _fmt_dt(date_begin, "gte"),
        "datePublishEnd": _fmt_dt(date_end, "lte"),
        "type": type, "number": number, "guid": guid,
        "IsAnnulled": _bool(is_annulled), "IsLocked": _bool(is_locked),
        "includeContent": _bool(include_content),
        "sort": sort, "limit": limit, "offset": offset,
    })
    return _request("GET", "/v1/messages", params=params).json()


def get_message(guid: str) -> dict:
    return _request("GET", f"/v1/messages/{guid}").json()


def get_message_files(guid: str, *, only_safe=True) -> bytes:
    r = _request("GET", f"/v1/messages/{guid}/files/archive",
                 params={"onlySafe": _bool(only_safe)}, stream=True)
    return r.content


def get_linked(guid: str) -> list:
    return _request("GET", f"/v1/messages/{guid}/linked").json()


# ── reports ──────────────────────────────────────────────────────────────────

def get_reports(*, bankrupt_guid=None, date_begin=None, date_end=None, type=None,
                number=None, guid=None, procedure_type=None, is_annulled=None,
                is_locked=None, include_content=False, sort="DatePublish:asc",
                limit=500, offset=0) -> dict:
    params = _clean({
        "bankruptGUID": bankrupt_guid,
        "datePublishBegin": _fmt_dt(date_begin, "gte"),
        "datePublishEnd": _fmt_dt(date_end, "lte"),
        "type": type, "number": number, "guid": guid,
        "procedureType": procedure_type,
        "IsAnnulled": _bool(is_annulled), "IsLocked": _bool(is_locked),
        "includeContent": _bool(include_content),
        "sort": sort, "limit": limit, "offset": offset,
    })
    return _request("GET", "/v1/reports", params=params).json()


def get_report(guid: str) -> dict:
    return _request("GET", f"/v1/reports/{guid}").json()


def get_report_files(guid: str, *, only_safe=True) -> bytes:
    r = _request("GET", f"/v1/reports/{guid}/files/archive",
                 params={"onlySafe": _bool(only_safe)}, stream=True)
    return r.content


def get_report_linked(guid: str) -> list:
    return _request("GET", f"/v1/reports/{guid}/linked").json()


# ── bankrupts ────────────────────────────────────────────────────────────────

def search_bankrupts(*, type="Person", inn=None, snils=None, name=None,
                     birthdate=None, ogrn=None, ogrnip=None, guid=None,
                     limit=50, offset=0) -> dict:
    params = _clean({
        "type": type, "inn": inn, "snils": snils, "name": name,
        "birthdate": birthdate, "ogrn": ogrn, "ogrnip": ogrnip, "guid": guid,
        "limit": limit, "offset": offset,
    })
    return _request("GET", "/v1/bankrupts", params=params).json()


# ── пагинация ────────────────────────────────────────────────────────────────

def iter_all(fetch_fn, *, limit=500, **kwargs) -> Iterator[dict]:
    """Итерирует все pageData по limit/offset до total. fetch_fn — get_messages/get_reports."""
    offset = 0
    while True:
        data = fetch_fn(limit=limit, offset=offset, **kwargs)
        page = data.get("pageData") or []
        for item in page:
            yield item
        total = data.get("total") or 0
        offset += limit
        if offset >= total or not page:
            break
