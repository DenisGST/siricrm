# -*- coding: utf-8 -*-
"""Оркестрация генерации заявления о банкротстве + приложений.

generate_isk(service, employee, overrides, sro_id, template_id) -> GeneratedDocument
"""
import logging

from apps.crm import client_log
from apps.crm.models import LegalEntity
from apps.files.folder_utils import _mk, get_or_create_root
from apps.files.models import ClientFile, StoredFile
from apps.files.s3_utils import upload_file_to_s3

from . import isk_appendices, isk_context
from .isk_engine import render_isk_docx
from .isk_seed_data import DEFAULT_APPENDIX
from .models import GeneratedDocument, IskTemplate
from .pdf_utils import docx_to_pdf, merge_pdfs

log = logging.getLogger(__name__)

_DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class IskGenerationError(RuntimeError):
    pass


def build_appendix_list(flags, overrides):
    """Нумерованный перечень приложений (для раздела {appendix_list})."""
    selected = overrides.get("appendix_keys")  # None → по умолчанию
    items = []
    for a in DEFAULT_APPENDIX:
        if selected is not None and a["key"] not in selected:
            continue
        cond = a.get("cond")
        if selected is None and cond and not flags.get(cond):
            continue
        items.append(a["label"])
    # доп. пункты из overrides (свободный ввод)
    for extra in (overrides.get("appendix_extra") or []):
        if extra.strip():
            items.append(extra.strip())
    return "\n".join(f"{i}. {label};" for i, label in enumerate(items, 1))


def _store(data, *, filename, content_type, prefix="afd/isk"):
    bucket, key = upload_file_to_s3(data, prefix=prefix, filename=filename,
                                    content_type=content_type)
    return StoredFile.objects.create(bucket=bucket, key=key, filename=filename,
                                     content_type=content_type, size=len(data))


def _attach(client, stored, employee):
    root = get_or_create_root(client)
    folder = _mk(client, root, "Заявления в суд", "court_filings", 4)
    ClientFile.objects.create(folder=folder, stored_file=stored, name=stored.filename,
                              size=stored.size or 0, content_type=stored.content_type,
                              uploaded_by=employee)


def _ensure_isk_action_type():
    """Гарантируем ActionType 'isk_created' для событийки."""
    from apps.crm.models import ActionType
    ActionType.objects.get_or_create(
        code="isk_created",
        defaults={"name": "Сформировано заявление о банкротстве", "order": 35,
                  "is_manual": False},
    )
    client_log.invalidate_cache()


def generate_isk(service, employee=None, overrides=None, sro_id=None,
                 template_id=None, response_id=None):
    overrides = overrides or {}
    template = (IskTemplate.objects.filter(pk=template_id).first() if template_id
                else None) or IskTemplate.get_default()
    if template is None or not template.sections.exists():
        raise IskGenerationError("Не найден шаблон заявления. Засидьте его в АФД.")

    sro = LegalEntity.objects.filter(pk=sro_id).first() if sro_id else None
    response = None
    if response_id:
        response = service.questionnaire_responses.filter(pk=response_id).first()

    ctx, flags, creditors, warnings = isk_context.build_isk_context(
        service, overrides=overrides, sro=sro, response=response)
    ctx["appendix_list"] = build_appendix_list(flags, overrides)

    # 1. Тело заявления
    isk_docx = render_isk_docx(template, ctx, flags)
    chunks = [docx_to_pdf(isk_docx)]

    # 2. Приложения-формы
    sel = overrides.get("appendix_keys")
    def _on(key):
        return sel is None or key in sel
    if _on("creditors_form"):
        chunks.append(docx_to_pdf(isk_appendices.creditors_form_docx(ctx, creditors)))
    if _on("property_form"):
        chunks.append(docx_to_pdf(isk_appendices.property_form_docx(ctx, overrides)))
    if _on("petition_doc"):
        chunks.append(docx_to_pdf(isk_appendices.petition_docx(ctx)))

    final_pdf = merge_pdfs(chunks) if len(chunks) > 1 else chunks[0]

    # 3. Сохранение (редактируемый .docx заявления + сводный PDF пакета)
    base = f"Заявление о банкротстве — {ctx['debtor_full']}".strip(" —")
    docx_sf = _store(isk_docx, filename=f"{base}.docx", content_type=_DOCX_CT)
    pdf_sf = _store(final_pdf, filename=f"{base}.pdf", content_type="application/pdf")
    _attach(service.client, pdf_sf, employee)
    _attach(service.client, docx_sf, employee)

    # 4. Событийка
    try:
        _ensure_isk_action_type()
        warn = (f" (без реквизитов: {', '.join(warnings)})" if warnings else "")
        client_log.record_action(
            service.client, "isk_created",
            comment=f"Сформировано заявление о банкротстве (кредиторов: "
                    f"{len(creditors)}){warn}",
            employee=employee, stored_file=pdf_sf,
        )
    except Exception:
        log.exception("generate_isk: событийка не записалась")

    gen = GeneratedDocument.objects.create(
        client=service.client, service=service, docx_file=docx_sf, pdf_file=pdf_sf,
        title=base, created_by=employee,
    )
    return gen, warnings, creditors
