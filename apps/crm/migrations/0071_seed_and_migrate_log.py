"""Seed EventType + ActionType и миграция ClientEvent → ClientLogEntry.

После применения:
  - 22 EventType (включая 'client_created' — куда уезжают старые 'system'-записи
    с описанием «Добавлен в базу»);
  - 25 ActionType (включая 'call_client' — объединяет 'call_outgoing'+'call_result',
    'claim_filed' — поглощает 'iskotpravlen');
  - все ClientEvent скопированы в ClientLogEntry с правильным kind+FK.

Старая модель ClientEvent дропается в следующей миграции (0072).
"""
import uuid
from django.db import migrations


# ─── Справочники ──────────────────────────────────────────

EVENT_TYPES = [
    # (code, name, source, order, is_system, description)
    ("first_contact",      "Первое обращение",            "client",       10, True, ""),
    ("dialog_started",     "Начат диалог",                "client",       20, True, ""),
    ("file_received",      "Получен файл от клиента",     "client",       30, True, ""),
    ("incoming_call",      "Входящий звонок",             "client",       40, False, ""),
    ("lead_received",      "Получен лид",                 "client",       50, True, ""),
    ("hearing_scheduled",  "Назначено судебное заседание","court",        110, True, ""),
    ("procedure_started",  "Введена процедура",           "court",        120, True, ""),
    ("procedure_ended",    "Окончена процедура",          "court",        130, True, ""),
    ("arbitr_event",       "Событие арбитражного дела",   "court",        140, True, ""),
    ("letter_incoming",    "Получено входящее письмо",    "legal_entity", 210, True, ""),
    ("status_change",      "Смена статуса",               "system",       310, True, ""),
    ("employee_assigned",  "Назначен сотрудник",          "system",       320, True, ""),
    ("employee_removed",   "Сотрудник снят",              "system",       330, True, ""),
    ("dept_assigned",      "Передан в работу отдела",     "system",       340, True, ""),
    ("dialog_ended",       "Окончен диалог",              "system",       350, True, ""),
    ("reminder",           "Напоминание",                 "system",       360, True, ""),
    ("service_created",    "Услуга добавлена",            "system",       370, True,
        "Порождается действием service_create."),
    ("service_deleted",    "Услуга удалена",              "system",       380, True,
        "Порождается действием service_delete."),
    ("charge_overdue",     "Начисление просрочено",       "system",       390, True, ""),
    ("bubble_imported",    "Импортирован из Bubble",      "system",       400, True, ""),
    ("bubble_enriched",    "Данные дополнены из Bubble",  "system",       410, True, ""),
    ("client_created",     "Клиент добавлен в базу",      "system",       420, True, ""),
]

ACTION_TYPES = [
    # (code, name, order, is_system, spawns_event_code, description)
    ("client_identified",         "Идентификация клиента",        10, True, None, ""),
    ("note",                      "Заметка",                      20, True, None, ""),
    ("contract_created",          "Заключение договора",          30, True, None, ""),
    ("contract_terminated",       "Расторжение договора",         40, True, None, ""),
    ("claim_filed",               "Подача иска в суд",            50, True, None,
        "Объединяет старые 'claim_filed' и 'iskotpravlen'."),
    ("file_sent",                 "Отправка файла клиенту",       60, True, None, ""),
    ("letter_outgoing",           "Отправка исходящего письма",   70, True, None, ""),
    ("note_to_colleague",         "Сообщение коллеге",            80, True, None, ""),
    ("call_client",               "Звонок клиенту",               90, True, None,
        "Объединяет старые 'call_outgoing' и 'call_result'."),
    ("service_create",            "Создание услуги",             100, True, "service_created",
        "Порождает событие service_created."),
    ("service_delete",            "Удаление услуги",             110, True, "service_deleted",
        "Порождает событие service_deleted."),
    ("consultation_book",         "Запись на консультацию",      120, True, None, ""),
    ("consultation_result_record","Запись результата консультации",130, True, None, ""),
    ("consultation_transfer",     "Перенос консультации",        140, True, None, ""),
    ("consultation_edit",         "Изменение консультации",      150, True, None, ""),
    ("questionnaire_create",      "Создание анкеты",             160, True, None, ""),
    ("questionnaire_edit",        "Редактирование анкеты",       170, True, None, ""),
    ("questionnaire_delete",      "Удаление анкеты",             180, True, None, ""),
    ("schedule_create",           "Составление графика платежей",190, True, None, ""),
    ("schedule_update",           "Изменение графика платежей",  200, True, None, ""),
    ("payment_in_create",         "Внесение входящего платежа",  210, True, None, ""),
    ("payment_in_edit",           "Редактирование входящего платежа", 220, True, None, ""),
    ("payment_in_delete",         "Удаление входящего платежа",  230, True, None, ""),
    ("payment_out_create",        "Внесение исходящего платежа", 240, True, None, ""),
    ("payment_out_edit",          "Редактирование исходящего платежа", 250, True, None, ""),
    ("payment_out_delete",        "Удаление исходящего платежа", 260, True, None, ""),
]

# Маппинг старых ClientEvent.event_type → (kind, code в новых справочниках)
# kind = 'event' → EventType code; kind = 'action' → ActionType code.
LEGACY_MAP = {
    # События
    "first_contact":      ("event",  "first_contact"),
    "status_change":      ("event",  "status_change"),
    "employee_assigned":  ("event",  "employee_assigned"),
    "employee_removed":   ("event",  "employee_removed"),
    "dept_assigned":      ("event",  "dept_assigned"),
    "hearing_scheduled":  ("event",  "hearing_scheduled"),
    "procedure_started":  ("event",  "procedure_started"),
    "procedure_ended":    ("event",  "procedure_ended"),
    "arbitr_event":       ("event",  "arbitr_event"),
    "dialog_started":     ("event",  "dialog_started"),
    "dialog_ended":       ("event",  "dialog_ended"),
    "file_received":      ("event",  "file_received"),
    "letter_incoming":    ("event",  "letter_incoming"),
    "reminder":           ("event",  "reminder"),
    "service_created":    ("event",  "service_created"),
    "service_deleted":    ("event",  "service_deleted"),
    "charge_overdue":     ("event",  "charge_overdue"),
    "bubble_imported":    ("event",  "bubble_imported"),
    "bubble_enriched":    ("event",  "bubble_enriched"),
    "lead_received":      ("event",  "lead_received"),
    "system":             ("event",  "client_created"),  # описание «Добавлен в базу»
    # Действия
    "client_identified":      ("action", "client_identified"),
    "note":                   ("action", "note"),
    "contract_created":       ("action", "contract_created"),
    "contract_terminated":    ("action", "contract_terminated"),
    "claim_filed":            ("action", "claim_filed"),
    "iskotpravlen":           ("action", "claim_filed"),       # merge → claim_filed
    "file_sent":              ("action", "file_sent"),
    "letter_outgoing":        ("action", "letter_outgoing"),
    "note_to_colleague":      ("action", "note_to_colleague"),
    "call_outgoing":          ("action", "call_client"),       # merge
    "call_result":            ("action", "call_client"),       # merge
    "consultation_booked":      ("action", "consultation_book"),
    "consultation_result":      ("action", "consultation_result_record"),
    "consultation_transferred": ("action", "consultation_transfer"),
    "consultation_edited":      ("action", "consultation_edit"),
    "questionnaire_created":  ("action", "questionnaire_create"),
    "questionnaire_edited":   ("action", "questionnaire_edit"),
    "questionnaire_deleted":  ("action", "questionnaire_delete"),
    "schedule_created":       ("action", "schedule_create"),
    "schedule_updated":       ("action", "schedule_update"),
    "payment_in_created":     ("action", "payment_in_create"),
    "payment_in_edited":      ("action", "payment_in_edit"),
    "payment_in_deleted":     ("action", "payment_in_delete"),
    "payment_out_created":    ("action", "payment_out_create"),
    "payment_out_edited":     ("action", "payment_out_edit"),
    "payment_out_deleted":    ("action", "payment_out_delete"),
}


def forwards(apps, schema_editor):
    EventType    = apps.get_model("crm", "EventType")
    ActionType   = apps.get_model("crm", "ActionType")
    ClientEvent  = apps.get_model("crm", "ClientEvent")
    ClientLogEntry = apps.get_model("crm", "ClientLogEntry")

    # 1) Создаём справочники (idempotent через get_or_create)
    et_by_code = {}
    for code, name, source, order, is_system, description in EVENT_TYPES:
        obj, _ = EventType.objects.update_or_create(
            code=code,
            defaults=dict(name=name, source=source, order=order,
                          is_system=is_system, description=description, is_active=True),
        )
        et_by_code[code] = obj

    at_by_code = {}
    for code, name, order, is_system, spawns_code, description in ACTION_TYPES:
        spawns = et_by_code.get(spawns_code) if spawns_code else None
        obj, _ = ActionType.objects.update_or_create(
            code=code,
            defaults=dict(name=name, order=order, is_system=is_system,
                          description=description, is_active=True,
                          spawns_event=spawns),
        )
        at_by_code[code] = obj

    # 2) Копируем ClientEvent → ClientLogEntry
    # Идемпотентность: если уже мигрировали (есть запись с таким же legacy ID),
    # не дублируем. Чтобы не плодить колонку — используем bubble_id как
    # «временный» маркер: для не-bubble записей пишем туда f"legacy:{ev.id}".
    # Это нужно ТОЛЬКО для повторного выполнения миграции в случае отката.
    already = set(
        ClientLogEntry.objects.filter(bubble_id__startswith="legacy:")
        .values_list("bubble_id", flat=True)
    )

    bulk = []
    BULK_SIZE = 1000
    n_skipped = 0
    n_unknown_type = 0

    for ev in ClientEvent.objects.all().iterator(chunk_size=2000):
        marker = f"legacy:{ev.id}"
        if marker in already:
            n_skipped += 1
            continue
        mapping = LEGACY_MAP.get(ev.event_type)
        if not mapping:
            # Неизвестный legacy event_type — мигрируем в action 'note' с пометкой
            n_unknown_type += 1
            mapping = ("action", "note")
        kind, code = mapping
        if kind == "event":
            event_type_obj = et_by_code.get(code)
            action_type_obj = None
            if event_type_obj is None:
                continue
        else:
            action_type_obj = at_by_code.get(code)
            event_type_obj = None
            if action_type_obj is None:
                continue

        # Bubble-импортированный ClientEvent имел заполненный bubble_id — сохраним
        # его (вместо legacy-маркера), чтобы повторный bubble-апдейт находил запись.
        bid = ev.bubble_id if ev.bubble_id else marker

        bulk.append(ClientLogEntry(
            id=uuid.uuid4(),
            bubble_id=bid,
            subject_kind="client",
            client_id=ev.client_id,
            kind=kind,
            event_type=event_type_obj,
            action_type=action_type_obj,
            comment=ev.description or "",
            old_value=ev.old_value or "",
            new_value=ev.new_value or "",
            employee_id=ev.employee_id,
            parent=None,
            # created_at — auto_now_add, патчим ниже через bulk update
        ))
        if len(bulk) >= BULK_SIZE:
            _flush_bulk(ClientLogEntry, bulk, ClientEvent)
            bulk.clear()
    if bulk:
        _flush_bulk(ClientLogEntry, bulk, ClientEvent)
        bulk.clear()

    print(f"  skipped (уже мигрированы): {n_skipped}")
    print(f"  unknown legacy event_type: {n_unknown_type}")


def _flush_bulk(ClientLogEntry, bulk, ClientEvent):
    # bulk_create НЕ обходит auto_now_add → created_at будет = now(). Чтобы
    # сохранить оригинальную дату исходного ClientEvent, делаем ручной update.
    # Маппим: legacy:<ev.id> → исходный ev.created_at.
    ClientLogEntry.objects.bulk_create(bulk)
    # Соберём legacy id из marker'ов
    legacy_ids = []
    for e in bulk:
        if e.bubble_id and e.bubble_id.startswith("legacy:"):
            try:
                legacy_ids.append(e.bubble_id.split(":", 1)[1])
            except IndexError:
                pass
    if not legacy_ids:
        return
    # Достанем оригинальные created_at одним запросом
    orig = dict(
        ClientEvent.objects.filter(pk__in=legacy_ids).values_list("pk", "created_at")
    )
    for e in bulk:
        if e.bubble_id and e.bubble_id.startswith("legacy:"):
            try:
                lid = e.bubble_id.split(":", 1)[1]
            except IndexError:
                continue
            cat = orig.get(uuid.UUID(lid))
            if cat:
                ClientLogEntry.objects.filter(pk=e.pk).update(created_at=cat)


def reverse(apps, schema_editor):
    # Откат — удаляем все записи лога и справочников.
    ClientLogEntry = apps.get_model("crm", "ClientLogEntry")
    ActionType = apps.get_model("crm", "ActionType")
    EventType = apps.get_model("crm", "EventType")
    ClientLogEntry.objects.all().delete()
    ActionType.objects.all().delete()
    EventType.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0070_create_log_models"),
    ]

    operations = [
        migrations.RunPython(forwards, reverse),
    ]
