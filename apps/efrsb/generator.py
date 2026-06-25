"""Генератор ТЕКСТА сообщения ЕФРСБ (на движке АФД).

build_efrsb_context(publication, overrides) — плоский dict плейсхолдеров (должник /
АУ / дело / суд + поля события). generate_publication(...) — рендер .docx (шаблон
DocumentTemplate kind=efrsb или секционный IskTemplate) → плоский текст + PDF, подшивка
в файл-менеджер клиента (папка «Публикации ЕФРСБ»), событийка.

Текст нужен для РУЧНОЙ публикации АУ в ЛК fedresurs (авто-публикация — Phase B).
Переиспользует AFD: render_docx / render_isk_docx / docx_to_pdf и helpers
apps.procedure.request_documents (_fmt, _debtor_address, _spouse_data).
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.afd.docx_engine import render_docx
from apps.afd.pdf_utils import docx_to_pdf
from apps.crm import client_log
from apps.files.folder_utils import _mk, get_or_create_root
from apps.files.models import ClientFile, StoredFile
from apps.files.s3_utils import download_file_from_s3, upload_file_to_s3
from apps.procedure.request_documents import _debtor_address, _fmt, _spouse_data

log = logging.getLogger(__name__)
DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Ключ поля «тело сообщения» в overrides (вводится АУ в форме генерации).
BODY_KEY = "Текст сообщения"


class EfrsbGenError(RuntimeError):
    pass


def _publication_procedure(publication):
    """Процедура для реквизитов: явная у публикации → текущая → последняя с АУ."""
    case = publication.case
    if publication.procedure_id and publication.procedure:
        return publication.procedure
    if case.current_procedure_id and case.current_procedure:
        return case.current_procedure
    return (case.procedures.exclude(arbitr_manager=None).order_by("-order").first()
            or case.procedures.order_by("-order").first())


def build_efrsb_context(publication, *, overrides=None) -> dict:
    """Плоский dict плейсхолдеров (Russian keys, как в request_documents)."""
    overrides = dict(overrides or {})
    case = publication.case
    client = case.service.client
    proc = _publication_procedure(publication)
    am = proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None
    arb = getattr(case.service, "arbitr_case", None)
    idx, addr_reg = _debtor_address(client)
    title = (publication.message_type.name if publication.message_type_id
             else (publication.title or "Сообщение"))

    ctx = {
        # Заголовок + тело (тело вводит АУ)
        "Заголовок": title,
        BODY_KEY: "",
        # Должник
        "Фамилия": client.last_name or "", "Имя": client.first_name or "",
        "Отчество": client.patronymic or "",
        "дата рождения": _fmt(client.birth_date), "место рождения": client.birth_place or "",
        "СНИЛС": client.snils or "", "ИНН": client.inn or "",
        "индекс": idx, "адрес регистрации": addr_reg,
        # Финуправляющий (АУ) — публикатор
        "ФИО Финансовый управляющий": am.full_fio if am else "",
        "ФамилияИО АУ": am.short_fio if am else "",
        "ИНН АУ": am.inn if am else "", "СНИЛС АУ": am.snils if am else "",
        "Адрес арбитражного управляющего": am.corr_address if am else "",
        "Телефон арбитражного": am.phone if am else "",
        "email арбитражного": am.email if am else "",
        "Реквизиты СРО": am.sro_display if am else "",
        # Дело / суд / процедура
        "арбитражный суд": (arb.court_name if arb else ""),
        "номер дела": (arb.case_number if arb else ""),
        "вид процедуры": (proc.get_kind_display() if proc else ""),
        "дата решения": _fmt(proc.intro_date) if proc else "",
        "срок процедуры": (str(proc.term_months) if proc and proc.term_months else ""),
        "дата публикации ЕФРСБ": _fmt(proc.publication_efrsb_date) if proc else "",
        # Супруг (на случай шаблонов, где нужен)
        "данные на супруга": _spouse_data(client.spouse),
        # Дата формирования
        "дата": _fmt(timezone.localdate()),
    }
    # overrides (включая «Текст сообщения») переопределяют авто-значения.
    ctx.update({k: v for k, v in overrides.items() if v is not None})
    return ctx


# Карта плейсхолдеров → (раздел, метка) для предпроверки (как _PH_MAP в запросах).
_PH_MAP = [
    ("Должник", [
        ("Фамилия", "Фамилия"), ("Имя", "Имя"), ("Отчество", "Отчество"),
        ("дата рождения", "Дата рождения"), ("место рождения", "Место рождения"),
        ("СНИЛС", "СНИЛС"), ("ИНН", "ИНН"), ("адрес регистрации", "Адрес регистрации"),
    ]),
    ("Финуправляющий (АУ)", [
        ("ФИО Финансовый управляющий", "ФИО ФУ"), ("ФамилияИО АУ", "Фамилия И.О. ФУ"),
        ("ИНН АУ", "ИНН ФУ"), ("СНИЛС АУ", "СНИЛС ФУ"),
        ("Адрес арбитражного управляющего", "Адрес корреспонденции ФУ"),
        ("Реквизиты СРО", "СРО"),
    ]),
    ("Дело и суд", [
        ("арбитражный суд", "Арбитражный суд"), ("номер дела", "Номер дела"),
        ("вид процедуры", "Вид процедуры"), ("дата решения", "Дата введения процедуры"),
    ]),
    ("Сообщение", [
        (BODY_KEY, "Текст сообщения"),
    ]),
]


def check_publication_data(publication, *, overrides=None):
    """Предпроверка плейсхолдеров (ok/✗) для модалки генерации. (all_ok, groups)."""
    ctx = build_efrsb_context(publication, overrides=overrides)
    used = set(ctx.keys())
    tpl = publication.message_type.template if (
        publication.message_type_id and publication.message_type.template_id) else None
    if tpl and tpl.stored_file_id:
        try:
            from apps.afd.docx_engine import list_placeholders
            tb = download_file_from_s3(tpl.stored_file.bucket, tpl.stored_file.key)
            used = set(list_placeholders(tb))
        except Exception:
            log.exception("check_publication_data: не прочитать плейсхолдеры шаблона")

    def _check(key):
        val = (ctx.get(key) or "").strip()
        if key == BODY_KEY:
            return {"value": val, "ok": bool(val),
                    "note": "" if val else "введите текст сообщения в форме ниже"}
        if not val:
            return {"value": "", "ok": False, "note": "не заполнено"}
        return {"value": val, "ok": True, "note": ""}

    groups, all_ok, known = [], True, set()
    for gname, items in _PH_MAP:
        rows = []
        for key, label in items:
            known.add(key)
            if key not in used:
                continue
            chk = _check(key)
            chk["label"] = label
            rows.append(chk)
            all_ok = all_ok and chk["ok"]
        if rows:
            groups.append({"name": gname, "rows": rows})
    return all_ok, groups


def _store(file_bytes, *, filename, content_type):
    bucket, key = upload_file_to_s3(
        file_bytes, prefix="efrsb/messages", filename=filename, content_type=content_type,
    )
    return StoredFile.objects.create(
        bucket=bucket, key=key, filename=filename, content_type=content_type, size=len(file_bytes),
    )


def _attach(client, stored, employee):
    root = get_or_create_root(client)
    folder = _mk(client, root, "Публикации ЕФРСБ", "efrsb", 6)
    ClientFile.objects.create(
        folder=folder, stored_file=stored, name=stored.filename,
        size=stored.size or 0, content_type=stored.content_type, uploaded_by=employee,
    )


def _extract_plain_text(docx_bytes: bytes) -> str:
    """Плоский текст документа (для копирования в ЛК fedresurs)."""
    import io
    from docx import Document
    from apps.afd.docx_engine import _iter_paragraphs
    doc = Document(io.BytesIO(docx_bytes))
    lines = [p.text for p in _iter_paragraphs(doc)]
    # схлопываем хвостовые пустые строки
    text = "\n".join(lines).strip()
    return text


def generate_publication(publication, *, overrides=None, employee=None):
    """Сформировать текст сообщения ЕФРСБ. Возвращает publication (с content_*)."""
    mt = publication.message_type
    if mt is None:
        raise EfrsbGenError("Не выбран тип сообщения.")
    overrides = dict(overrides or {})

    ctx = build_efrsb_context(publication, overrides=overrides)

    # Рендер: секционный IskTemplate либо .docx DocumentTemplate.
    if mt.isk_template_id and mt.isk_template:
        from apps.afd.isk_engine import render_isk_docx
        docx_bytes = render_isk_docx(mt.isk_template, ctx, {})
    elif mt.template_id and mt.template and mt.template.stored_file_id:
        template_bytes = download_file_from_s3(
            mt.template.stored_file.bucket, mt.template.stored_file.key)
        docx_bytes = render_docx(template_bytes, ctx)
    else:
        raise EfrsbGenError(
            "У типа сообщения не задан шаблон текста. Привяжите .docx (kind=ЕФРСБ) "
            "в справочнике «Типы сообщений ЕФРСБ» или в разделе АФД."
        )

    generated_text = _extract_plain_text(docx_bytes)
    try:
        pdf_bytes = docx_to_pdf(docx_bytes)
    except Exception as exc:  # noqa: BLE001
        raise EfrsbGenError(f"Не удалось сконвертировать в PDF: {exc}") from exc

    title = ctx.get("Заголовок") or mt.name
    base = f"ЕФРСБ — {title}"[:120]
    docx_sf = _store(docx_bytes, filename=f"{base}.docx", content_type=DOCX_CT)
    pdf_sf = _store(pdf_bytes, filename=f"{base}.pdf", content_type="application/pdf")

    client = publication.case.service.client
    _attach(client, pdf_sf, employee)
    _attach(client, docx_sf, employee)

    publication.title = title
    publication.generated_text = generated_text
    publication.content_docx = docx_sf
    publication.content_pdf = pdf_sf
    publication.overrides = overrides
    publication.generated_at = timezone.now()
    publication.created_by = employee
    if publication.status == publication.STATUS_DRAFT:
        publication.status = publication.STATUS_GENERATED
    publication.save(update_fields=[
        "title", "generated_text", "content_docx", "content_pdf", "overrides",
        "generated_at", "created_by", "status", "updated_at",
    ])

    try:
        client_log.invalidate_cache()
        client_log.record_action(
            client, "efrsb_text_generated",
            comment=f"Сформирован текст сообщения ЕФРСБ: {title}. "
                    f"Файл — в папке «Публикации ЕФРСБ».",
            employee=employee, stored_file=pdf_sf,
        )
    except Exception:
        log.exception("generate_publication: не удалось записать событийку")
    return publication
