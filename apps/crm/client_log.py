"""Хелперы записи в `ClientLogEntry` (единый лог клиента).

Используется во всём коде CRM вместо прямого `ClientEvent.objects.create(...)`
(старая модель удалена в миграции 0072). Концепция:

    События (events) — что произошло, источник = system/court/client/legal/employee.
    Действия (actions) — что сделал сотрудник; могут порождать события
    (через `ActionType.spawns_event` — auto-create event с parent=action).

Базовое использование::

    from apps.crm import client_log
    client_log.record_event(client, "first_contact", comment="...")
    client_log.record_action(client, "call_client", comment="...", employee=emp)

Для совместимости со старым api (`event_type` строкой) — хелпер
`record_legacy(client, event_type=..., description=..., employee=...)`
самостоятельно классифицирует по справочникам и пишет в нужный kind.

Все коды справочников — в миграции `0071_seed_and_migrate_log.py`.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Кэш справочников (заполняется лениво при первом доступе) — справочники
# меняются редко, кэш на процесс. При обновлении через UI сбрасывается вручную.
_EVENT_TYPE_CACHE: dict[str, "EventType"] = {}
_ACTION_TYPE_CACHE: dict[str, "ActionType"] = {}


def _et(code: str):
    """Достать EventType по коду (с кэшем)."""
    from apps.crm.models import EventType
    cached = _EVENT_TYPE_CACHE.get(code)
    if cached is not None:
        return cached
    obj = EventType.objects.filter(code=code).first()
    if obj is not None:
        _EVENT_TYPE_CACHE[code] = obj
    return obj


def _at(code: str):
    """Достать ActionType по коду (с кэшем)."""
    from apps.crm.models import ActionType
    cached = _ACTION_TYPE_CACHE.get(code)
    if cached is not None:
        return cached
    obj = ActionType.objects.filter(code=code).first()
    if obj is not None:
        _ACTION_TYPE_CACHE[code] = obj
    return obj


def invalidate_cache():
    """Сбросить кэш справочников. Дёргать после правок в /references/."""
    _EVENT_TYPE_CACHE.clear()
    _ACTION_TYPE_CACHE.clear()


def record_event(
    client,
    code: str,
    *,
    comment: str = "",
    employee=None,
    parent=None,
    old_value: str = "",
    new_value: str = "",
    bubble_id: Optional[str] = None,
    stored_file=None,
):
    """Записать событие в лог клиента.

    code — code из EventType (см. миграцию 0071). Если справочник не найден,
    лог пишется как WARNING и возвращается None (не падаем).

    stored_file — опциональный StoredFile (для событий «Получен файл»),
    чтобы в модалке лога вывести имя файла + ссылку на предпросмотр.
    """
    from apps.crm.models import ClientLogEntry

    if client is None:
        return None
    et = _et(code)
    if et is None:
        logger.warning("EventType code=%r не найден; событие не записано", code)
        return None
    return ClientLogEntry.objects.create(
        subject_kind="client",
        client=client,
        kind="event",
        event_type=et,
        comment=comment or "",
        employee=employee,
        parent=parent,
        old_value=str(old_value)[:255],
        new_value=str(new_value)[:255],
        bubble_id=bubble_id,
        stored_file=stored_file,
    )


def record_action(
    client,
    code: str,
    *,
    comment: str = "",
    employee=None,
    parent=None,
    old_value: str = "",
    new_value: str = "",
    bubble_id: Optional[str] = None,
    stored_file=None,
):
    """Записать действие. Если у ActionType задан spawns_event — автоматически
    создаётся событие с parent=созданное действие.

    stored_file — опциональный StoredFile (для действий «Отправлен файл»).
    """
    from apps.crm.models import ClientLogEntry

    if client is None:
        return None
    at = _at(code)
    if at is None:
        logger.warning("ActionType code=%r не найден; действие не записано", code)
        return None
    action = ClientLogEntry.objects.create(
        subject_kind="client",
        client=client,
        kind="action",
        action_type=at,
        comment=comment or "",
        employee=employee,
        parent=parent,
        old_value=str(old_value)[:255],
        new_value=str(new_value)[:255],
        bubble_id=bubble_id,
        stored_file=stored_file,
    )
    # Spawn связанное событие, если задано
    if at.spawns_event_id is not None:
        ClientLogEntry.objects.create(
            subject_kind="client",
            client=client,
            kind="event",
            event_type_id=at.spawns_event_id,
            comment=comment or "",
            employee=employee,
            parent=action,
        )
    return action


# ─── Совместимость со старым API: классификация по legacy event_type ──────

# Старые ClientEvent.event_type → (kind, new_code). Совпадает с LEGACY_MAP
# из миграции 0071. Дублируем здесь чтобы рефакторинг старых call-сайтов
# не требовал ручной классификации в каждом месте.
_LEGACY_MAP = {
    "first_contact":            ("event",  "first_contact"),
    "status_change":            ("event",  "status_change"),
    "employee_assigned":        ("event",  "employee_assigned"),
    "employee_removed":         ("event",  "employee_removed"),
    "dept_assigned":            ("event",  "dept_assigned"),
    "hearing_scheduled":        ("event",  "hearing_scheduled"),
    "procedure_started":        ("event",  "procedure_started"),
    "procedure_ended":          ("event",  "procedure_ended"),
    "arbitr_event":             ("event",  "arbitr_event"),
    "dialog_started":           ("event",  "dialog_started"),
    "dialog_ended":             ("event",  "dialog_ended"),
    "file_received":            ("event",  "file_received"),
    "letter_incoming":          ("event",  "letter_incoming"),
    "reminder":                 ("event",  "reminder"),
    "service_created":          ("event",  "service_created"),
    "service_deleted":          ("event",  "service_deleted"),
    "charge_overdue":           ("event",  "charge_overdue"),
    "bubble_imported":          ("event",  "bubble_imported"),
    "bubble_enriched":          ("event",  "bubble_enriched"),
    "lead_received":            ("event",  "lead_received"),
    "system":                   ("event",  "client_created"),
    "client_identified":        ("action", "client_identified"),
    "note":                     ("action", "note"),
    "contract_created":         ("action", "contract_created"),
    "contract_terminated":      ("action", "contract_terminated"),
    "claim_filed":              ("action", "claim_filed"),
    "iskotpravlen":             ("action", "claim_filed"),
    "file_sent":                ("action", "file_sent"),
    "letter_outgoing":          ("action", "letter_outgoing"),
    "note_to_colleague":        ("action", "note_to_colleague"),
    "call_outgoing":            ("action", "call_client"),
    "call_result":              ("action", "call_client"),
    "consultation_booked":      ("action", "consultation_book"),
    "consultation_result":      ("action", "consultation_result_record"),
    "consultation_transferred": ("action", "consultation_transfer"),
    "consultation_edited":      ("action", "consultation_edit"),
    "questionnaire_created":    ("action", "questionnaire_create"),
    "questionnaire_edited":     ("action", "questionnaire_edit"),
    "questionnaire_deleted":    ("action", "questionnaire_delete"),
    "schedule_created":         ("action", "schedule_create"),
    "schedule_updated":         ("action", "schedule_update"),
    "payment_in_created":       ("action", "payment_in_create"),
    "payment_in_edited":        ("action", "payment_in_edit"),
    "payment_in_deleted":       ("action", "payment_in_delete"),
    "payment_out_created":      ("action", "payment_out_create"),
    "payment_out_edited":       ("action", "payment_out_edit"),
    "payment_out_deleted":      ("action", "payment_out_delete"),
}


def record_legacy(
    client,
    event_type: str,
    *,
    description: str = "",
    employee=None,
    old_value: str = "",
    new_value: str = "",
    bubble_id: Optional[str] = None,
):
    """Совместимость со старым API: принимает legacy event_type строкой,
    сам решает кладёт event или action.

    Используется чтобы рефакторинг 12+ старых call-сайтов делался единичной
    заменой `ClientEvent.objects.create(client=, event_type=, description=)`
    → `client_log.record_legacy(client, event_type, description=)`.
    """
    mapping = _LEGACY_MAP.get(event_type)
    if mapping is None:
        logger.warning(
            "record_legacy: неизвестный legacy event_type=%r → пишем как 'note'",
            event_type,
        )
        mapping = ("action", "note")
    kind, code = mapping
    fn = record_event if kind == "event" else record_action
    return fn(
        client, code,
        comment=description, employee=employee,
        old_value=old_value, new_value=new_value, bubble_id=bubble_id,
    )
