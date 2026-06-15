"""Celery-задачи раздела процедур: контроль сроков мероприятий."""
from celery import shared_task
from django.utils import timezone

from apps.crm import client_log

from .models import ProcedureMilestone


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
