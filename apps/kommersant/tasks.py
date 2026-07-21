"""Celery-задачи «Коммерсанта» (дефолтная очередь — БЕЗ роутинга, браузер не нужен).

🛑 Вся сетевая работа с почтой живёт ЗДЕСЬ, а не во вьюхах: SMTP-отправка письма
   с вложениями и IMAP-обход ящика занимают секунды-десятки секунд, а sync-threadpool
   daphne на таком уже вешался (инцидент WA-вебхука 09.06.2026).
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.cache import cache

from . import services
from .mailer import KommersantMailError
from .models import KOMMERSANT_EMAIL, KommersantPublication

log = logging.getLogger(__name__)


@shared_task(name="kommersant.send_request")
def send_request(publication_id: str, employee_id: int | None = None,
                 to_addr: str = KOMMERSANT_EMAIL) -> str:
    """Отправить заявку в ИД (кнопка «Запросить счёт»)."""
    publication = (
        KommersantPublication.objects
        .filter(pk=publication_id)
        .select_related("case__service__client", "procedure", "blank_pdf", "blank_docx")
        .first()
    )
    if publication is None:
        return "no_publication"

    # Двойной клик по кнопке не должен отправить в ИД две заявки на одну публикацию.
    lock_key = f"kommersant:send:{publication_id}"
    if not cache.add(lock_key, "1", 600):
        return "locked"
    try:
        employee = None
        if employee_id:
            from apps.core.models import Employee
            employee = Employee.objects.filter(pk=employee_id).first()
        services.send_request(publication, employee=employee, to_addr=to_addr)
        return "sent"
    except KommersantMailError as exc:
        log.warning("kommersant send_request %s: %s", publication_id, exc)
        return f"error: {exc}"
    finally:
        cache.delete(lock_key)


@shared_task(name="kommersant.poll_invoices")
def poll_invoices() -> str:
    """Обойти ящики сотрудников-АУ и забрать счета по отправленным заявкам (beat).

    Ходим только к тем, у кого реально есть ожидающие заявки, — иначе на каждом
    тике дёргали бы IMAP всех АУ подряд без повода.
    """
    from apps.core.models import Employee
    from apps.procedure.models import ArbitrationManager

    pending = (
        KommersantPublication.objects
        .filter(status=KommersantPublication.STATUS_SENT, invoice_received_at__isnull=True)
        .exclude(sent_message_id="")
        .select_related("procedure")
    )
    manager_ids = {
        p.procedure.arbitr_manager_id for p in pending
        if p.procedure_id and p.procedure.arbitr_manager_id
    }
    if not manager_ids:
        return "nothing to poll"

    employee_ids = set(
        ArbitrationManager.objects
        .filter(pk__in=manager_ids, employee__isnull=False)
        .values_list("employee_id", flat=True)
    )
    if not employee_ids:
        return "no mailboxes"

    total = 0
    for emp in Employee.objects.filter(pk__in=employee_ids, is_active=True).select_related("user"):
        if not emp.kommersant_mail_configured:
            continue
        lock_key = f"kommersant:imap:{emp.pk}"
        if not cache.add(lock_key, "1", 600):
            continue
        try:
            total += services.sync_invoices(emp)
        except KommersantMailError as exc:
            log.warning("kommersant poll_invoices emp=%s: %s", emp.pk, exc)
        except Exception:
            log.exception("kommersant poll_invoices emp=%s упал", emp.pk)
        finally:
            cache.delete(lock_key)
    return f"invoices: {total}"


@shared_task(name="kommersant.fetch_invoice_now")
def fetch_invoice_now(publication_id: str) -> str:
    """Ручная проверка почты по одной заявке («Проверить счёт сейчас»)."""
    publication = (
        KommersantPublication.objects.filter(pk=publication_id)
        .select_related("case__service__client", "procedure")
        .first()
    )
    if publication is None:
        return "no_publication"
    am = services.manager_for(publication)
    if am is None or not am.employee_id:
        return "no_manager"
    emp = am.employee
    lock_key = f"kommersant:imap:{emp.pk}"
    if not cache.add(lock_key, "1", 600):
        return "locked"
    try:
        return f"invoices: {services.sync_invoices(emp)}"
    except KommersantMailError as exc:
        return f"error: {exc}"
    finally:
        cache.delete(lock_key)
