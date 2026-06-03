"""DaData Suggest API для юрлиц/ИП.

Используется при импорте Bubble Organization, когда юрлица нет в нашем
справочнике LegalEntity по ИНН — тогда тянем реквизиты из DaData и создаём
новую запись.

Два метода:
- find_by_inn(inn)   — точное совпадение по ИНН (https://dadata.ru/api/find-party/)
- search_by_name(q)  — fuzzy-search по названию (https://dadata.ru/api/suggest/party/)

Оба возвращают нормализованный dict с полями LegalEntity, либо None.
Запрашиваемые поля DaData: name (short_with_opf/full_with_opf), inn, kpp,
ogrn, okpo, okved, address (value), management.name/post, type (LEGAL|INDIVIDUAL).
"""
from __future__ import annotations

import logging
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger("bubble_import")

_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
_FIND_BY_ID_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"


def _headers() -> dict:
    api_key = getattr(settings, "DADATA_API_KEY", "")
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {api_key}",
    }


def _normalize(party: dict) -> dict:
    """Bubble→Django: схлопнуть ответ DaData в поля модели LegalEntity."""
    data = party.get("data") or {}
    name_block = data.get("name") or {}
    address = data.get("address") or {}
    management = data.get("management") or {}
    state = data.get("state") or {}

    # ИП vs ЮЛ
    dadata_type = (data.get("type") or "").upper()  # "LEGAL" | "INDIVIDUAL"
    opf_short = ((data.get("opf") or {}).get("short") or "").lower()
    entity_type = "ip" if dadata_type == "INDIVIDUAL" else {
        "ооо": "ooo", "оао": "other", "пао": "pao", "ао": "ao",
    }.get(opf_short, "other")

    status_map = {
        "ACTIVE": "active", "LIQUIDATING": "liquidation",
        "BANKRUPT": "bankruptcy", "LIQUIDATED": "liquidated",
    }
    status = status_map.get(state.get("status") or "", "active")

    return {
        "name": name_block.get("full_with_opf") or party.get("value") or "",
        "short_name": name_block.get("short_with_opf") or "",
        "brand": name_block.get("latin") or "",
        "inn": data.get("inn") or "",
        "kpp": data.get("kpp") or "",
        "ogrn": data.get("ogrn") or "",
        "okpo": data.get("okpo") or "",
        "okved": data.get("okved") or "",
        "legal_address": address.get("unrestricted_value") or address.get("value") or "",
        "director_name": management.get("name") or "",
        "director_title": management.get("post") or "",
        "entity_type": entity_type,
        "status": status,
    }


def find_by_inn(inn: str) -> Optional[dict]:
    """Точное совпадение по ИНН (10 для ЮЛ, 12 для ИП).

    Возвращает нормализованный dict с реквизитами для LegalEntity или None.
    Берёт первое actively-зарегистрированное юрлицо (DaData ранжирует:
    свежее → выше).
    """
    if not getattr(settings, "DADATA_API_KEY", ""):
        logger.warning("DaData API key не задан — пропускаем lookup по ИНН")
        return None
    inn = "".join(c for c in str(inn) if c.isdigit())
    if len(inn) not in (10, 12):
        return None
    try:
        resp = requests.post(
            _FIND_BY_ID_URL, json={"query": inn, "count": 1},
            headers=_headers(), timeout=10,
        )
        resp.raise_for_status()
        suggestions = resp.json().get("suggestions") or []
    except Exception as e:
        logger.warning(f"DaData findById failed for inn={inn}: {e}")
        return None
    if not suggestions:
        return None
    return _normalize(suggestions[0])


def search_by_name(query: str) -> Optional[dict]:
    """Fuzzy-поиск по названию. Возвращает топ-1 результат или None.

    Полезно когда у Bubble Organization нет ИНН или он невалидный, но есть
    осмысленное название (например, «ПАО Сбербанк»).
    """
    if not getattr(settings, "DADATA_API_KEY", ""):
        logger.warning("DaData API key не задан — пропускаем поиск по имени")
        return None
    query = (query or "").strip()
    if not query:
        return None
    try:
        resp = requests.post(
            _SUGGEST_URL,
            json={"query": query, "count": 1, "status": ["ACTIVE", "LIQUIDATING"]},
            headers=_headers(), timeout=10,
        )
        resp.raise_for_status()
        suggestions = resp.json().get("suggestions") or []
    except Exception as e:
        logger.warning(f"DaData suggest failed for {query!r}: {e}")
        return None
    if not suggestions:
        return None
    return _normalize(suggestions[0])
