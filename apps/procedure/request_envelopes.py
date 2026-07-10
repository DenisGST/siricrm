"""Почтовые конверты (Почта России) для запросов в госорганы.

Тонкая обвязка над движком `apps/afd/envelope.py`: строит конверт(ы) для
запроса/пакета (адресат — из `Request`, отправитель — из `ExecutorOrg`),
подшивает PDF в папку клиента «Конверты» и отдаёт байты для печати/просмотра.
"""
from __future__ import annotations

import re

from apps.afd.envelope import (
    DEFAULT_SIZE, SIZES, party_from_manager, party_from_request, render_envelope,
    render_envelopes, sender_from_executor,
)
from apps.files.folder_utils import _mk, get_or_create_root
from apps.files.models import ClientFile, StoredFile
from apps.files.s3_utils import upload_file_to_s3

_SLUG_RE = re.compile(r"[^\w\-. ]+", re.UNICODE)


def _safe(text: str, limit: int = 40) -> str:
    return _SLUG_RE.sub("", (text or "").strip())[:limit].strip() or "адресат"


def _valid_size(size: str) -> str:
    return size if size in SIZES else DEFAULT_SIZE


def _sender_for_case(case):
    """Отправитель конверта — актуальный ФУ дела; фолбэк — реквизиты Исполнителя."""
    from .request_documents import _am_procedure
    proc = _am_procedure(case)
    am = proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None
    if am is not None:
        p = party_from_manager(am)
        if p.get("name"):
            return p
    return sender_from_executor()


def _store_pdf(pdf_bytes: bytes, *, filename: str) -> StoredFile:
    bucket, key = upload_file_to_s3(
        pdf_bytes, prefix="procedure/envelopes",
        filename=filename, content_type="application/pdf",
    )
    return StoredFile.objects.create(
        bucket=bucket, key=key, filename=filename,
        content_type="application/pdf", size=len(pdf_bytes),
    )


def _attach(client, stored: StoredFile, employee):
    """Подшить в папку клиента «Конверты» (без дублей по имени)."""
    root = get_or_create_root(client)
    folder = _mk(client, root, "Конверты", "envelopes", 6)
    if ClientFile.objects.filter(folder=folder, name=stored.filename).exists():
        return
    ClientFile.objects.create(
        folder=folder, stored_file=stored, name=stored.filename,
        size=stored.size or 0, content_type=stored.content_type, uploaded_by=employee,
    )


def envelope_for_request(req, *, size=DEFAULT_SIZE, employee=None, store=True) -> bytes:
    """Один конверт для запроса. Возвращает PDF-байты (+ подшивает в файлы клиента)."""
    size = _valid_size(size)
    sender = _sender_for_case(req.case)
    recipient = party_from_request(req)
    pdf = render_envelope(sender, recipient, size=size)
    if store:
        num = req.outgoing_number or "—"
        filename = f"Конверт №{num} {_safe(recipient.get('name'))} ({size}).pdf"
        stored = _store_pdf(pdf, filename=filename)
        _attach(req.case.service.client, stored, employee)
    return pdf


def envelopes_for_case(case, *, size=DEFAULT_SIZE, employee=None, store=True):
    """Конверты для всех запросов дела, у которых есть адресат.

    Возвращает (pdf_bytes | None, made, skipped). skipped — запросы без адресата.
    """
    size = _valid_size(size)
    reqs = list(case.requests.select_related("recipient", "request_type")
                .order_by("outgoing_number"))
    parties, made, skipped = [], 0, 0
    for r in reqs:
        p = party_from_request(r)
        if not p.get("name"):
            skipped += 1
            continue
        parties.append(p)
        made += 1
    if not parties:
        return None, 0, skipped
    sender = _sender_for_case(case)
    pdf = render_envelopes(sender, parties, size=size)
    if store:
        filename = f"Конверты дела ({made} шт, {size}).pdf"
        stored = _store_pdf(pdf, filename=filename)
        _attach(case.service.client, stored, employee)
    return pdf, made, skipped
