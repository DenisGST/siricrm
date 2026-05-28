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

# WhatsApp-сообщения и Files импортируем только за последние N лет.
MESSAGEWSP_YEARS = 3
FILES_YEARS = 3


def _entity_constraints(entity: str) -> list | None:
    """Серверные фильтры Bubble для конкретной сущности."""
    if entity == "MessageWSP":
        cutoff = timezone.now() - datetime.timedelta(days=365 * MESSAGEWSP_YEARS)
        return [{
            "key": "Created Date",
            "constraint_type": "greater than",
            "value": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }]
    if entity == "Files":
        cutoff = timezone.now() - datetime.timedelta(days=365 * FILES_YEARS)
        return [{
            "key": "Created Date",
            "constraint_type": "greater than",
            "value": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }]
    return None


def fetch_modified_since(entity: str, since: datetime.datetime) -> dict:
    """Доливка: выгрузить записи entity, изменённые ПОСЛЕ `since`.

    Фильтр по `Modified Date` (а не Created Date) — попадают и новые,
    и обновлённые в Bubble записи. Bubble Data API «greater than» —
    сдвигаем `since` на 1 сек назад для покрытия границы.
    """
    constraints = [{
        "key": "Modified Date",
        "constraint_type": "greater than",
        "value": (since - datetime.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }]
    cursor = 0
    created = updated = 0
    while True:
        page = bubble_api.fetch_page(
            entity, cursor=cursor, limit=bubble_api.PAGE_LIMIT,
            constraints=constraints,
        )
        results = page["results"]
        if not results:
            break
        for obj in results:
            bid = obj.get("_id")
            if not bid:
                continue
            display = extract_display(entity, obj)
            if entity == "ProjectBFL":
                from . import resolvers
                display["display_status"] = resolvers.lookup(
                    "StatusPrj", obj.get("statusPrj"), "nameStatusPrj",
                )
            _, is_new = BubbleRecord.objects.update_or_create(
                entity=entity, bubble_id=bid,
                defaults={"raw": obj, **display},
            )
            if is_new:
                created += 1
            else:
                updated += 1
        cursor += len(results)
        if page.get("remaining", 0) <= 0:
            break

    state = get_state(entity)
    state.total_fetched = BubbleRecord.objects.filter(entity=entity).count()
    state.last_fetch_at = timezone.now()
    state.save(update_fields=["total_fetched", "last_fetch_at"])

    logger.info(
        "Bubble fetch_modified_since %s since %s: +%d new, %d updated",
        entity, since.date(), created, updated,
    )
    return {"created": created, "updated": updated}


def _window_constraints(entity: str, start: datetime.datetime,
                        end: datetime.datetime) -> list:
    """Constraints для конкретного временного окна. Bubble Data API
    поддерживает только `greater than` / `less than` (без `or equal`),
    поэтому нижнюю границу сдвигаем на секунду назад — даёт 1-секундный
    overlap между соседними окнами, но fetch_window идемпотентен через
    update_or_create, дубликаты безопасны."""
    start_excl = start - datetime.timedelta(seconds=1)
    return [
        {"key": "Created Date", "constraint_type": "greater than",
         "value": start_excl.strftime("%Y-%m-%dT%H:%M:%SZ")},
        {"key": "Created Date", "constraint_type": "less than",
         "value": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
    ]


def fetch_window(entity: str, start: datetime.datetime,
                 end: datetime.datetime) -> dict:
    """Выгрузить ВСЕ записи entity за окно [start, end). Локальный cursor,
    state.cursor не трогаем. update_or_create обеспечивает идемпотентность
    при повторных запусках."""
    constraints = _window_constraints(entity, start, end)
    cursor = 0
    created = updated = 0
    fetched_total = 0
    while True:
        page = bubble_api.fetch_page(
            entity, cursor=cursor, limit=bubble_api.PAGE_LIMIT,
            constraints=constraints,
        )
        results = page["results"]
        if not results:
            break
        for obj in results:
            bid = obj.get("_id")
            if not bid:
                continue
            display = extract_display(entity, obj)
            if entity == "ProjectBFL":
                from . import resolvers
                display["display_status"] = resolvers.lookup(
                    "StatusPrj", obj.get("statusPrj"), "nameStatusPrj",
                )
            _, is_new = BubbleRecord.objects.update_or_create(
                entity=entity, bubble_id=bid,
                defaults={"raw": obj, **display},
            )
            if is_new:
                created += 1
            else:
                updated += 1
        got = len(results)
        cursor += got
        fetched_total += got
        if page.get("remaining", 0) <= 0:
            break

    # Актуализируем state, чтобы цифра в UI не отставала.
    state = get_state(entity)
    state.total_fetched = BubbleRecord.objects.filter(entity=entity).count()
    state.last_fetch_at = timezone.now()
    state.save(update_fields=["total_fetched", "last_fetch_at"])

    logger.info(
        "Bubble fetch_window %s [%s..%s]: +%d new, %d upd, total in window %d",
        entity, start.date(), end.date(), created, updated, fetched_total,
    )
    return {"created": created, "updated": updated, "fetched": fetched_total}


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
            # Для услуг — резолвим название статуса (statusPrj) в отдельный столбец.
            if entity == "ProjectBFL":
                from . import resolvers
                display["display_status"] = resolvers.lookup(
                    "StatusPrj", obj.get("statusPrj"), "nameStatusPrj",
                )
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
