"""FETCH-логика: постраничная выгрузка Bubble → staging-таблица BubbleRecord."""
import datetime
import logging

from django.utils import timezone

from . import bubble_api
from .extractors import extract_display
from .models import BubbleRecord, BubbleFetchState

logger = logging.getLogger("bubble_import")

# Сколько записей тянуть за одно нажатие «Загрузить ещё».
DEFAULT_BATCH = 50

# WhatsApp-сообщения импортируем только за последние N лет.
MESSAGEWSP_YEARS = 3


def _entity_constraints(entity: str) -> list | None:
    """Серверные фильтры Bubble для конкретной сущности."""
    if entity == "MessageWSP":
        cutoff = timezone.now() - datetime.timedelta(days=365 * MESSAGEWSP_YEARS)
        return [{
            "key": "Created Date",
            "constraint_type": "greater than",
            "value": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }]
    return None


def get_state(entity: str) -> BubbleFetchState:
    state, _ = BubbleFetchState.objects.get_or_create(entity=entity)
    return state


def fetch_batch(entity: str, batch: int = DEFAULT_BATCH) -> dict:
    """Выкачать очередную порцию объectов entity начиная с сохранённого курсора.

    Возвращает сводку: {fetched, created, updated, remaining, total}.
    Идемпотентно — повторные объекты обновляются по (entity, bubble_id).
    """
    state = get_state(entity)
    cursor = state.cursor
    fetched = created = updated = 0
    remaining = state.total_remote
    constraints = _entity_constraints(entity)

    while fetched < batch:
        want = min(bubble_api.PAGE_LIMIT, batch - fetched)
        page = bubble_api.fetch_page(entity, cursor=cursor, limit=want,
                                     constraints=constraints)
        results = page["results"]
        remaining = page["remaining"]
        if not results:
            break

        for obj in results:
            bid = obj.get("_id")
            if not bid:
                continue
            display = extract_display(entity, obj)
            rec, is_new = BubbleRecord.objects.update_or_create(
                entity=entity, bubble_id=bid,
                defaults={"raw": obj, **display},
            )
            if is_new:
                created += 1
            else:
                updated += 1
        got = len(results)
        fetched += got
        cursor += got
        if remaining <= 0:
            break

    total = remaining + cursor
    state.cursor = cursor
    state.total_remote = total
    state.total_fetched = BubbleRecord.objects.filter(entity=entity).count()
    state.last_fetch_at = timezone.now()
    state.save()

    logger.info(
        "Bubble fetch %s: +%d new, %d upd, cursor=%d, remaining=%d",
        entity, created, updated, cursor, remaining,
    )
    return {
        "fetched": fetched, "created": created, "updated": updated,
        "remaining": remaining, "total": total,
        "total_fetched": state.total_fetched,
    }
