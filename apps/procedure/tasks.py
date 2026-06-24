"""Celery-задачи раздела процедур: контроль сроков мероприятий и ответов на запросы."""
from celery import shared_task
from django.utils import timezone

from apps.crm import client_log

from .models import ProcedureMilestone, Request


@shared_task(name="procedure.mark_overdue_milestones")
def mark_overdue_milestones():
    """Пометить просроченные мероприятия и уведомить сотрудников.

    pending + due_date < today → overdue + событийка `procedure_milestone_overdue`
    (EventType с notifies=True сам рассылает уведомления). Каждое мероприятие
    флипается один раз → ровно одно уведомление.
    """
    today = timezone.localdate()
    qs = (
        ProcedureMilestone.objects.filter(
            status=ProcedureMilestone.STATUS_PENDING,
            due_date__lt=today,
        )
        .select_related("case__service__client")
    )
    count = 0
    for ms in qs.iterator():
        ms.status = ProcedureMilestone.STATUS_OVERDUE
        ms.save(update_fields=["status", "updated_at"])
        client = ms.case.service.client
        client_log.record_event(
            client,
            "procedure_milestone_overdue",
            comment=(
                f"Просрочено мероприятие: {ms.title} "
                f"(срок {ms.due_date:%d.%m.%Y})"
            ),
        )
        count += 1
    return count


@shared_task(name="procedure.mark_overdue_requests")
def mark_overdue_requests():
    """Уведомить о просроченных ответах на запросы.

    Отправленные запросы без ответа с due_date < today → событийка
    `request_overdue` (EventType с notifies=True рассылает уведомления).
    Флаг overdue_notified — чтобы уведомить ровно один раз.
    """
    today = timezone.localdate()
    qs = (
        Request.objects.filter(
            status=Request.STATUS_SENT,
            due_date__lt=today,
            overdue_notified=False,
        )
        .select_related("case__service__client", "recipient")
    )
    count = 0
    for r in qs.iterator():
        r.overdue_notified = True
        r.save(update_fields=["overdue_notified", "updated_at"])
        client_log.record_event(
            r.case.service.client,
            "request_overdue",
            comment=(
                f"Просрочен ответ на запрос: {r.title} → {r.recipient_display} "
                f"(срок {r.due_date:%d.%m.%Y})"
            ),
        )
        count += 1
    return count
