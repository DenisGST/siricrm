"""Оркестрация публикаций в «Коммерсантъ»: создание, отправка, приём счёта.

Чистые функции над моделями — вся сетевая работа делегируется mailer.py, вызовы
из UI идут через tasks.py (Celery), чтобы SMTP/IMAP не выполнялись в ASGI.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.crm import client_log
from apps.efrsb.generator import _procedure_for
from apps.files.folder_utils import _mk, get_or_create_root
from apps.files.models import ClientFile, StoredFile
from apps.files.s3_utils import upload_file_to_s3

from . import mailer
from .mail_accounts import account_for_employee, account_for_manager
from .models import KOMMERSANT_EMAIL, KommersantPublication

log = logging.getLogger(__name__)


def create_publication(case, message_type, *, procedure=None, employee=None):
    """Завести заготовку публикации (текст добавляется следующим шагом)."""
    proc = procedure or _procedure_for(case)
    return KommersantPublication.objects.create(
        case=case,
        procedure=proc,
        message_type=message_type,
        status=KommersantPublication.STATUS_DRAFT,
        title=message_type.name if message_type else "",
        created_by=employee,
    )


def manager_for(publication):
    """АУ, от чьего имени подаётся заявка и на чей ящик придёт счёт."""
    proc = publication.procedure or _procedure_for(publication.case)
    return proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None


def mail_account_for(publication):
    """Почтовый аккаунт АУ (из его профиля) для этой публикации или None."""
    return account_for_manager(manager_for(publication))


def mail_account_for_manager(am):
    """Почтовый аккаунт по арбитражному управляющему (для контекста UI)."""
    return account_for_manager(am)


def send_request(publication, *, employee=None, to_addr: str = KOMMERSANT_EMAIL):
    """Отправить заявку в ИД (задача «запрос счёта»).

    Ошибку транспорта сохраняем в `send_error` — иначе после падения Celery-таски
    юрист видит вечное «отправляется» и не понимает, что случилось.
    """
    account = mail_account_for(publication)
    try:
        mailer.send_request(publication, account, to_addr=to_addr, employee=employee)
    except mailer.KommersantMailError as exc:
        publication.send_error = str(exc)
        publication.save(update_fields=["send_error", "updated_at"])
        raise

    try:
        client_log.invalidate_cache()
        client_log.record_action(
            publication.case.service.client, "kommersant_request_sent",
            comment=f"Заявка на публикацию в «Коммерсантъ» отправлена на {to_addr}. "
                    f"Ожидаем счёт на оплату.",
            employee=employee, stored_file=publication.blank_pdf,
        )
    except Exception:
        log.exception("send_request: не удалось записать событийку")
    return publication


# ── приём счёта ─────────────────────────────────────────────────────────────

def _store_invoice(publication, filename: str, content_type: str, data: bytes) -> StoredFile:
    bucket, key = upload_file_to_s3(
        data, prefix="kommersant/invoices", filename=filename,
        content_type=content_type or "application/octet-stream",
    )
    stored = StoredFile.objects.create(
        bucket=bucket, key=key, filename=filename,
        content_type=content_type or "application/octet-stream", size=len(data),
    )
    client = publication.case.service.client
    root = get_or_create_root(client)
    folder = _mk(client, root, "Публикации Коммерсантъ", "kommersant", 7)
    ClientFile.objects.create(
        folder=folder, stored_file=stored, name=filename,
        size=len(data), content_type=stored.content_type, uploaded_by=None,
    )
    return stored


def ingest_invoice(publication, reply: dict) -> bool:
    """Привязать пришедший счёт к публикации. True — если что-то записали.

    Идемпотентно: повторный разбор того же письма ничего не меняет (сверяем
    Message-ID ответа), иначе поллер плодил бы дубли счетов при каждом проходе.
    """
    reply_id = (reply.get("reply_message_id") or "").strip()
    if reply_id and publication.invoice_message_id == reply_id:
        return False
    if publication.invoice_file_id and not reply_id:
        return False

    files = reply.get("files") or []
    if not files:
        return False

    meta = mailer.parse_invoice_meta(reply.get("subject") or "", reply.get("body") or "")

    # Счётом считаем первое вложение-документ; картинки-подписи из письма пропускаем.
    stored = None
    for filename, ctype, data in files:
        if (ctype or "").startswith("image/"):
            continue
        stored = _store_invoice(publication, filename, ctype, data)
        break
    if stored is None:
        return False

    publication.invoice_file = stored
    publication.invoice_message_id = reply_id
    publication.invoice_received_at = reply.get("date") or timezone.now()
    if meta.get("number"):
        publication.invoice_number = meta["number"]
    if meta.get("date"):
        publication.invoice_date = meta["date"]
    if meta.get("amount") is not None:
        publication.invoice_amount = meta["amount"]
    if publication.status == KommersantPublication.STATUS_SENT:
        publication.status = KommersantPublication.STATUS_INVOICED
    publication.save()

    amount = f" на {publication.invoice_amount} ₽" if publication.invoice_amount else ""
    number = f" № {publication.invoice_number}" if publication.invoice_number else ""
    try:
        client_log.invalidate_cache()
        client_log.record_action(
            publication.case.service.client, "kommersant_invoice_received",
            comment=f"Получен счёт{number}{amount} на публикацию в «Коммерсантъ». "
                    f"Файл — в папке «Публикации Коммерсантъ». Оплата должна дойти "
                    f"до 14:00 мск в дату окончания приёма сообщений.",
            employee=None, stored_file=stored,
        )
    except Exception:
        log.exception("ingest_invoice: не удалось записать событийку")
    return True


def sync_invoices(employee) -> int:
    """Забрать счета из ящика сотрудника-АУ по всем его ожидающим заявкам.

    Возвращает число новых счетов. Ящик один на сотрудника, поэтому опрашиваем его
    разом по всем публикациям, где он назначен ФУ.
    """
    account = account_for_employee(employee)
    if account is None or not account.has_imap:
        return 0

    pending = list(
        KommersantPublication.objects
        .filter(status=KommersantPublication.STATUS_SENT, invoice_received_at__isnull=True)
        .exclude(sent_message_id="")
        .select_related("case__service__client", "procedure")
    )
    pending = [
        p for p in pending
        if (m := manager_for(p)) and m.employee_id == employee.pk
    ]
    if not pending:
        return 0

    by_mid = {p.sent_message_id: p for p in pending}
    replies = mailer.fetch_replies(account, message_ids=set(by_mid))

    count = 0
    for reply in replies:
        publication = by_mid.get(reply["message_id"])
        if publication is None:
            continue
        if not mailer.looks_like_invoice(reply):
            continue
        try:
            if ingest_invoice(publication, reply):
                count += 1
        except Exception:
            log.exception("sync_invoices: не удалось разобрать счёт для %s", publication.pk)
    return count
