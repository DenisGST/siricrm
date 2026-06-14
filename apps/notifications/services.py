"""Генерация и обработка уведомлений. Точка входа — notify() из событийки."""
from django.utils import timezone

from apps.crm import client_log
from .models import Notification

# Реакция → (новый статус, код ActionType для записи в событийку)
RESPONSE_MAP = {
    "accept":      (Notification.STATUS_ACCEPTED,     "notif_accepted"),
    "done":        (Notification.STATUS_DONE,         "notif_done"),
    "reject":      (Notification.STATUS_REJECTED,     "notif_rejected"),
    "snooze":      (Notification.STATUS_SNOOZED,      "notif_snoozed"),
    "acknowledge": (Notification.STATUS_ACKNOWLEDGED, "notif_acknowledged"),
}


def recipients_for_client(client):
    """Сотрудники, работающие с клиентом — ориентир «Мой канбан».

    Union: ответственные (Client.employees) ∪ исполнители услуг
    (Service.employees) ∪ сотрудники отдела текущего этапа услуги.
    Совпадает с логикой Client.objects.visible_to для рядового сотрудника.

    🛑 Считаем тремя отдельными индексированными запросами и объединяем pk в
    Python. Один общий `filter(Q|Q|Q).distinct()` порождает join Employee ×
    услуги × отделы с OR — на боевой БД план взрывается (десятки секунд).
    """
    from apps.core.models import Employee
    from apps.crm.models import Service
    if client is None:
        return Employee.objects.none()

    ids: set = set()
    # 1) ответственные за клиента
    ids |= set(Employee.objects.filter(clients=client, is_active=True)
               .values_list("pk", flat=True))
    # 2) исполнители любой услуги клиента
    ids |= set(Employee.objects.filter(assigned_services__client=client, is_active=True)
               .values_list("pk", flat=True))
    # 3) сотрудники отдела текущего этапа услуги
    dept_ids = list(
        Service.objects.filter(client=client)
        .exclude(common_status__department__isnull=True)
        .values_list("common_status__department_id", flat=True)
    )
    if dept_ids:
        ids |= set(Employee.objects.filter(department_id__in=dept_ids, is_active=True)
                   .values_list("pk", flat=True))

    return Employee.objects.filter(pk__in=ids)


def _build_text(entry) -> str:
    t = entry.event_type or entry.action_type
    base = t.name if t else "Событие"
    if entry.comment:
        base = f"{base}: {entry.comment}"
    return base[:500]


def notify(entry, recipients=None, *, exclude_actor=True):
    """Создать уведомления по записи событийки и разослать (WS + Telegram).

    recipients — явные получатели (адресные уведомления, напр. передача
    услуги конкретному сотруднику). Если None — союз «Мой канбан».
    exclude_actor — не уведомлять самого автора действия.
    """
    if entry is None or entry.client_id is None:
        return []
    t = entry.event_type or entry.action_type
    if recipients is None:
        recipients = recipients_for_client(entry.client)

    actor_id = entry.employee_id if exclude_actor else None
    text = _build_text(entry)
    hint = (t.notify_hint if t else "") or ""

    rows = [
        Notification(recipient=emp, client_id=entry.client_id, source=entry,
                     text=text, hint=hint)
        for emp in recipients
        if emp.pk != actor_id
    ]
    if not rows:
        return []
    Notification.objects.bulk_create(rows)  # PG возвращает pk

    # Рассылка. Импорт здесь — избегаем циклов и тянем channels лениво.
    from apps.realtime.utils import push_notification
    for n in rows:
        push_notification(n)
        # TODO stage C: enqueue_telegram(n)
    return rows


def respond(notification, action, *, employee=None, via="web", snooze_until=None, comment=""):
    """Реакция сотрудника на уведомление. Пишет действие в событийку и
    меняет статус. action ∈ {acknowledge, accept, done, reject, snooze}.

    comment — необязательный комментарий сотрудника (напр. причина отклонения);
    добавляется к тексту записи в событийке.
    """
    if action not in RESPONSE_MAP:
        raise ValueError(f"unknown action {action!r}")
    status, code = RESPONSE_MAP[action]

    log_comment = notification.text
    if comment:
        log_comment = f"{notification.text} — {comment.strip()}"

    # 🛑 отложку тоже фиксируем в событийке (требование ТЗ)
    log = client_log.record_action(
        notification.client, code,
        comment=log_comment, employee=employee, parent=notification.source,
    )
    notification.status = status
    notification.responded_via = via
    notification.response_log = log
    if action == "snooze":
        notification.snooze_until = snooze_until
    else:
        notification.responded_at = timezone.now()
        notification.snooze_until = None
    notification.save(update_fields=[
        "status", "responded_via", "response_log",
        "responded_at", "snooze_until",
    ])

    from apps.realtime.utils import push_notification_badge
    push_notification_badge(notification.recipient)  # обновить бейдж у получателя
    # TODO stage C: edit_telegram_card(notification)
    return notification
