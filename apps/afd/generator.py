"""Оркестрация генерации договора БФЛ: docx → pdf + приложения → S3 + событийка."""
import logging

from django.utils import timezone

from apps.crm import client_log
from apps.files.folder_utils import _mk, get_or_create_root
from apps.files.models import ClientFile, StoredFile
from apps.files.s3_utils import download_file_from_s3, upload_file_to_s3

from . import appendix, contract_bfl
from .docx_engine import render_docx
from .models import DocumentTemplate, ExecutorOrg, GeneratedDocument
from .pdf_utils import docx_to_pdf, merge_pdfs

log = logging.getLogger(__name__)


class ContractGenerationError(RuntimeError):
    pass


def _store(file_bytes, *, filename, content_type, prefix="contracts"):
    bucket, key = upload_file_to_s3(
        file_bytes, prefix=prefix, filename=filename, content_type=content_type,
    )
    return StoredFile.objects.create(
        bucket=bucket, key=key, filename=filename,
        content_type=content_type, size=len(file_bytes),
    )


def _attach_to_file_manager(client, stored, employee):
    root = get_or_create_root(client)
    folder = _mk(client, root, "Договоры", "contracts", 3)
    ClientFile.objects.create(
        folder=folder, stored_file=stored, name=stored.filename,
        size=stored.size or 0, content_type=stored.content_type, uploaded_by=employee,
    )


def generate_bfl_contract(service, employee=None):
    """Генерирует договор БФЛ (docx + сводный pdf с приложениями).

    Возвращает GeneratedDocument. Бросает ContractGenerationError при проблемах.
    """
    template = DocumentTemplate.active_for_kind(DocumentTemplate.KIND_CONTRACT_BFL)
    if template is None:
        raise ContractGenerationError(
            "Не найден активный шаблон договора БФЛ. Загрузите его в АФД."
        )
    if ExecutorOrg.get_default() is None:
        raise ContractGenerationError(
            "Не задана организация-исполнитель. Заполните реквизиты в АФД."
        )

    ok, _groups = contract_bfl.check_requisites(service)
    if not ok:
        raise ContractGenerationError("Не все обязательные реквизиты заполнены.")

    # 1. Шаблон из S3 → подстановка → docx
    template_bytes = download_file_from_s3(
        template.stored_file.bucket, template.stored_file.key
    )
    ctx = contract_bfl.build_context(service)
    docx_bytes = render_docx(template_bytes, ctx)

    # 2. Основной PDF + приложения → сводный PDF
    pdf_main = docx_to_pdf(docx_bytes)
    pdf_chunks = [pdf_main]
    # Согласие на обработку персональных данных (оператор — из ExecutorOrg).
    try:
        consent = appendix.consent_pdf(ctx)
        if consent:
            pdf_chunks.append(consent)
    except Exception:
        log.exception("generate_bfl_contract: не удалось сформировать согласие на ПДн")
    schedule_pdf = appendix.schedule_appendix_pdf(service, appendix_no=1)
    if schedule_pdf:
        pdf_chunks.append(schedule_pdf)
    quest_pdf = appendix.questionnaire_appendix_pdf(service)
    if quest_pdf:
        pdf_chunks.append(quest_pdf)
    final_pdf = merge_pdfs(pdf_chunks) if len(pdf_chunks) > 1 else pdf_main

    # 3. Сохранение в S3
    numb = service.numb_dogovor or "договор"
    base = f"Договор {numb}"
    docx_sf = _store(
        docx_bytes, filename=f"{base}.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    pdf_sf = _store(final_pdf, filename=f"{base}.pdf", content_type="application/pdf")

    # 4. Привязка: к услуге (PDF — полный пакет) + в файл-менеджер клиента
    service.contract_file = pdf_sf
    service.save(update_fields=["contract_file"])
    _attach_to_file_manager(service.client, pdf_sf, employee)
    _attach_to_file_manager(service.client, docx_sf, employee)

    # 5. Событийка
    try:
        client_log.record_action(
            service.client, "contract_created",
            comment=f"Сформирован договор {numb} с клиентом"
                    + (" (с приложениями)" if len(pdf_chunks) > 1 else "")
                    + ". Файлы (PDF и .docx) — в папке «Договоры» клиента.",
            employee=employee, stored_file=pdf_sf,
        )
    except Exception:
        log.exception("generate_bfl_contract: не удалось записать событийку")

    # 6. История АФД
    gen = GeneratedDocument.objects.create(
        template=template, client=service.client, service=service,
        docx_file=docx_sf, pdf_file=pdf_sf, title=base, created_by=employee,
    )
    log.info("Договор сформирован: service=%s numb=%s at %s",
             service.pk, numb, timezone.now())
    return gen
