"""Celery-задачи раздела процедур: контроль сроков + фоновое формирование
документов пакета запросов (с прогрессом для UI)."""
import logging

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

from apps.crm import client_log

from .models import ProcedureMilestone, Request

log = logging.getLogger(__name__)

# ── Пакетное формирование документов: прогресс в Redis ───────────────────────
# 🛑 Статус КАЖДОЙ позиции — в своём ключе, а не общим словарём на задание:
# документы формируются параллельными тасками, и они бы затирали друг другу
# записи при read-modify-write общего dict.
JOB_TTL = 60 * 60  # час — дольше UI прогресс не показывает


def job_meta_key(job_id):
    return f"procedure:pkg:{job_id}:meta"


def job_item_key(job_id, request_id):
    return f"procedure:pkg:{job_id}:item:{request_id}"


def job_status(job_id):
    """Свод по заданию для UI: (meta, items, counters) или (None, [], {})."""
    meta = cache.get(job_meta_key(job_id))
    if not meta:
        return None, [], {}
    items, counters = [], {"done": 0, "error": 0, "skip": 0, "work": 0, "wait": 0}
    for it in meta["items"]:
        st = cache.get(job_item_key(job_id, it["id"])) or {"status": "wait", "error": ""}
        counters[st["status"]] = counters.get(st["status"], 0) + 1
        items.append({**it, **st})
    counters["total"] = len(items)
    counters["finished"] = counters["done"] + counters["error"] + counters["skip"]
    counters["running"] = counters["finished"] < counters["total"]
    return meta, items, counters


@shared_task(name="procedure.generate_request_doc")
def generate_request_doc(job_id, request_id, employee_id=None, with_signature=False):
    """Сформировать документ ОДНОГО запроса пакета (задача на документ).

    🛑 Одна задача = один документ (а не весь пакет разом): у дефолтного воркера
    `--time-limit=300`, а конвертация LibreOffice занимает секунды — 18 штук
    подряд в лимит не влезут и задачу убьют на середине. Плюс так они идут
    параллельно (docx_to_pdf поднимает отдельный профиль LibreOffice — безопасно).
    """
    from apps.core.models import Employee

    from .request_documents import generate_request_document

    key = job_item_key(job_id, request_id)

    def mark(status, error=""):
        cache.set(key, {"status": status, "error": error}, JOB_TTL)

    mark("work")
    req = (Request.objects
           .select_related("request_type__template", "case__service__client", "recipient")
           .filter(pk=request_id).first())
    if req is None:
        mark("error", "запрос не найден (удалён?)")
        return
    if not (req.request_type_id and req.request_type.template_id):
        mark("skip", "нет шаблона документа")
        return
    emp = Employee.objects.filter(pk=employee_id).first() if employee_id else None
    try:
        generate_request_document(req, with_signature=with_signature, employee=emp)
    except Exception as exc:  # noqa: BLE001 — показываем причину юристу в прогрессе
        log.exception("Не удалось сформировать документ запроса %s", request_id)
        mark("error", str(exc)[:200])
        return
    mark("done")


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
