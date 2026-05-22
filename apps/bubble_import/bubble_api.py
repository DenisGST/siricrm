"""Клиент Bubble.io Data API.

Эндпоинт и токен — из env (см. .env.*):
    BUBBLE_API_BASE  — корень Data API, напр.
                       https://siricrmdev.ru/version-test/api/1.1/obj
    BUBBLE_API_TOKEN — Bearer-токен (Settings → API → API token).

Особенности Bubble Data API:
* пагинация через cursor + limit (макс 100 на запрос);
* в ответе только НЕпустые поля объекта;
* поле _id — уникальный идентификатор объекта;
* response.remaining — сколько ещё осталось после текущей страницы.
"""
import logging

import requests
from decouple import config

logger = logging.getLogger("bubble_import")

API_BASE = config(
    "BUBBLE_API_BASE",
    default="https://siricrmdev.ru/version-test/api/1.1/obj",
)
API_TOKEN = config("BUBBLE_API_TOKEN", default="")

PAGE_LIMIT = 100  # максимум, который отдаёт Bubble за один запрос


class BubbleAPIError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(API_BASE and API_TOKEN)


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_TOKEN}"}


def fetch_page(entity: str, cursor: int = 0, limit: int = PAGE_LIMIT) -> dict:
    """Одна страница объектов. Возвращает dict с ключами:
    results (list), remaining (int), cursor (int), count (int)."""
    url = f"{API_BASE}/{entity}"
    params = {"cursor": cursor, "limit": limit}
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=60)
    except requests.RequestException as e:
        raise BubbleAPIError(f"Сетевая ошибка при запросе {entity}: {e}") from e

    if resp.status_code != 200:
        raise BubbleAPIError(
            f"Bubble API {entity} вернул {resp.status_code}: {resp.text[:300]}"
        )

    try:
        payload = resp.json()["response"]
    except (ValueError, KeyError) as e:
        raise BubbleAPIError(f"Некорректный ответ Bubble для {entity}: {e}") from e

    return {
        "results": payload.get("results", []),
        "remaining": payload.get("remaining", 0),
        "cursor": payload.get("cursor", cursor),
        "count": payload.get("count", len(payload.get("results", []))),
    }


def count_total(entity: str) -> int:
    """Сколько всего объектов данного типа в Bubble (по первой странице)."""
    page = fetch_page(entity, cursor=0, limit=1)
    return page["remaining"] + page["count"]


def iter_all(entity: str, start_cursor: int = 0):
    """Генератор всех объектов типа — постранично. Для management-команд."""
    cursor = start_cursor
    while True:
        page = fetch_page(entity, cursor=cursor)
        results = page["results"]
        if not results:
            break
        for obj in results:
            yield obj
        if page["remaining"] <= 0:
            break
        cursor += len(results)
