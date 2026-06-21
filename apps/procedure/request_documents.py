"""Формирование документа-запроса (исходящее письмо): подстановка плейсхолдеров
из дела/должника/АУ/госоргана → .docx + PDF в файлы дела.

Переиспользует движок AFD (render_docx, docx_to_pdf, S3). Подпись/печать (PNG)
накладываются позже — когда заданы ArbitrationManager.signature_file/stamp_file.
"""
import logging
import re

from django.db.models import Max
from django.utils import timezone

from apps.afd.docx_engine import render_docx
from apps.afd.pdf_utils import docx_to_pdf
from apps.crm import client_log
from apps.files.folder_utils import _mk, get_or_create_root
from apps.files.models import ClientFile, StoredFile
from apps.files.s3_utils import download_file_from_s3, upload_file_to_s3

log = logging.getLogger(__name__)
DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class RequestDocError(RuntimeError):
    pass


def _fmt(d):
    return d.strftime("%d.%m.%Y") if d else ""


def _spouse_data(spouse) -> str:
    if spouse is None:
        return ""
    fio = " ".join(filter(None, [spouse.last_name, spouse.first_name, spouse.patronymic])).strip()
    parts = []
    if spouse.birth_date:
        parts.append(f"дата рождения {_fmt(spouse.birth_date)}")
    if spouse.inn:
        parts.append(f"ИНН {spouse.inn}")
    if spouse.snils:
        parts.append(f"СНИЛС {spouse.snils}")
    return f"{fio} ({', '.join(parts)})" if parts else fio


def _debtor_address(client):
    addr = (client.addresses.filter(address_type="registration").first()
            or client.addresses.first())
    if not addr:
        return "", ""
    return (addr.postal_code or ""), (addr.result or addr.source or "")


def _am_procedure(case):
    """Процедура, чей ФУ берём для реквизитов (актуальная с назначенным АУ)."""
    return (case.procedures.exclude(arbitr_manager=None).order_by("-order").first()
            or case.current_procedure
            or case.procedures.order_by("-order").first())


def build_request_context(req, *, marriage_cert="", gen_date=None) -> dict:
    case = req.case
    client = case.service.client
    proc = _am_procedure(case)
    am = proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None
    rec = req.recipient
    arb = getattr(case.service, "arbitr_case", None)
    idx, addr_reg = _debtor_address(client)
    gen_date = gen_date or timezone.localdate()
    rec_addr = ""
    if rec:
        rec_addr = rec.legal_address or rec.actual_address or rec.postal_address or ""
    return {
        # Должник
        "Фамилия": client.last_name or "", "Имя": client.first_name or "",
        "Отчество": client.patronymic or "",
        "дата рождения": _fmt(client.birth_date), "место рождения": client.birth_place or "",
        "СНИЛС": client.snils or "", "ИНН": client.inn or "",
        "индекс": idx, "адрес регистрации": addr_reg,
        # Финуправляющий (АУ)
        "ФИО Финансовый управляющий": am.full_fio if am else "",
        "ФамилияИО АУ": am.short_fio if am else "",
        "ИНН АУ": am.inn if am else "", "СНИЛС АУ": am.snils if am else "",
        "Адрес арбитражного управляющего": am.corr_address if am else "",
        "Телефон арбитражного": am.phone if am else "", "email арбитражного": am.email if am else "",
        "Реквизиты СРО": am.sro_display if am else "",
        # Дело / суд
        "арбитражный суд": (arb.court_name if arb else ""),
        "номер дела": (arb.case_number if arb else ""),
        "дата решения": _fmt(proc.intro_date) if proc else "",
        "срок процедуры": (str(proc.term_months) if proc and proc.term_months else ""),
        # Запрос (исходящее)
        "Исх.№": str(req.outgoing_number) if req.outgoing_number else "",
        "Исх.дата": _fmt(gen_date),
        "Адресат": (rec.name if rec else (req.recipient_name or "")),
        "Адрес": rec_addr,
        # Супруг
        "данные на супруга": _spouse_data(client.spouse),
        "свидетельство о браке": marriage_cert or "",
    }


def _apply_signature(docx_bytes: bytes, am) -> bytes:
    """Вставить один PNG (подпись+печать вместе) в строку подписи ФУ — после
    «Финансовый управляющий», перед ФИО. Нет картинки — вернуть как есть."""
    import io
    sig = (download_file_from_s3(am.signature_file.bucket, am.signature_file.key)
           if am and am.signature_file_id else None)
    if not sig:
        return docx_bytes
    from docx import Document
    from docx.shared import Cm
    doc = Document(io.BytesIO(docx_bytes))
    label = "Финансовый управляющий"
    target = None
    for p in doc.paragraphs:
        if label in p.text:
            target = p  # последняя строка подписи
    if target is None or not target.runs:
        return docx_bytes
    full = "".join(r.text for r in target.runs)
    idx = full.find(label)
    rest = full[idx + len(label):] if idx >= 0 else ""
    target.runs[0].text = label + " "
    for r in target.runs[1:]:
        r.text = ""
    target.add_run().add_picture(io.BytesIO(sig), width=Cm(5))
    target.add_run(" " + rest)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# Карта плейсхолдеров → (раздел, человекочитаемая метка) для предпроверки.
_PH_MAP = [
    ("Должник", [
        ("Фамилия", "Фамилия"), ("Имя", "Имя"), ("Отчество", "Отчество"),
        ("дата рождения", "Дата рождения"), ("место рождения", "Место рождения"),
        ("СНИЛС", "СНИЛС"), ("ИНН", "ИНН"),
        ("индекс", "Индекс (адрес)"), ("адрес регистрации", "Адрес регистрации"),
    ]),
    ("Финуправляющий (АУ)", [
        ("ФИО Финансовый управляющий", "ФИО ФУ"), ("ФамилияИО АУ", "Фамилия И.О. ФУ"),
        ("ИНН АУ", "ИНН ФУ"), ("СНИЛС АУ", "СНИЛС ФУ"),
        ("Адрес арбитражного управляющего", "Адрес корреспонденции ФУ"),
        ("Телефон арбитражного", "Телефон ФУ"), ("email арбитражного", "E-mail ФУ"),
        ("Реквизиты СРО", "СРО"),
    ]),
    ("Дело и суд", [
        ("арбитражный суд", "Арбитражный суд"), ("номер дела", "Номер дела"),
        ("дата решения", "Дата решения"), ("срок процедуры", "Срок процедуры, мес."),
    ]),
    ("Адресат (госорган)", [
        ("Адресат", "Адресат"), ("Адрес", "Адрес госоргана"),
    ]),
    ("Супруг", [
        ("данные на супруга", "Данные супруга"),
        ("свидетельство о браке", "Свидетельство о браке"),
    ]),
    ("Исходящее", [
        ("Исх.№", "Исходящий №"), ("Исх.дата", "Дата исходящего"),
    ]),
]
_AUTO_KEYS = {"Исх.№", "Исх.дата"}  # присваиваются автоматически при формировании


def check_request_data(req):
    """Предпроверка данных для подстановки: какие плейсхолдеры шаблона заполнены
    (ok) и каких нет / неверный формат. Возвращает (all_ok, groups)."""
    ctx = build_request_context(req)
    used = set(ctx.keys())
    tpl = req.request_type.template if req.request_type_id else None
    if tpl and tpl.stored_file_id:
        try:
            from apps.afd.docx_engine import list_placeholders
            tb = download_file_from_s3(tpl.stored_file.bucket, tpl.stored_file.key)
            used = set(list_placeholders(tb))
        except Exception:
            log.exception("check_request_data: не удалось прочитать плейсхолдеры шаблона")

    def _check(key):
        val = (ctx.get(key) or "").strip()
        if key in _AUTO_KEYS:
            return {"value": "присвоится автоматически", "ok": True, "note": ""}
        if key == "свидетельство о браке":
            return {"value": "", "ok": True, "note": "вводится в форме ниже"}
        if not val:
            return {"value": "", "ok": False, "note": "не заполнено"}
        if key in ("ИНН", "ИНН АУ") and len(re.sub(r"\D", "", val)) not in (10, 12):
            return {"value": val, "ok": False, "note": "неверный формат ИНН"}
        if key in ("СНИЛС", "СНИЛС АУ") and len(re.sub(r"\D", "", val)) != 11:
            return {"value": val, "ok": False, "note": "неверный формат СНИЛС"}
        if key == "email арбитражного" and "@" not in val:
            return {"value": val, "ok": False, "note": "неверный e-mail"}
        return {"value": val, "ok": True, "note": ""}

    groups, all_ok = [], True
    known = set()
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
    extra = sorted(k for k in used if k not in known)
    if extra:
        rows = []
        for key in extra:
            chk = _check(key)
            chk["label"] = key
            rows.append(chk)
            all_ok = all_ok and chk["ok"]
        groups.append({"name": "Прочее", "rows": rows})
    return all_ok, groups


def _has_image(p):
    from docx.oxml.ns import qn
    return bool(p._element.findall(".//" + qn("w:drawing")))


def extract_editable_paragraphs(docx_bytes: bytes) -> list:
    """Абзацы документа для редактирования (непустые, без картинок).
    index — позиция в стабильном обходе (тот же, что в apply)."""
    import io
    from docx import Document
    from apps.afd.docx_engine import _iter_paragraphs
    doc = Document(io.BytesIO(docx_bytes))
    out = []
    for i, p in enumerate(_iter_paragraphs(doc)):
        if _has_image(p):
            continue
        if p.text.strip():
            out.append({"index": i, "text": p.text})
    return out


def apply_paragraph_edits(docx_bytes: bytes, edits: dict) -> bytes:
    """Применить правки текста абзацев (edits: {index: new_text}). Текст пишется
    в первый run абзаца (его формат сохраняется), остальные очищаются. Абзацы с
    картинками не трогаются."""
    import io
    from docx import Document
    from apps.afd.docx_engine import _iter_paragraphs
    doc = Document(io.BytesIO(docx_bytes))
    for i, p in enumerate(_iter_paragraphs(doc)):
        if i not in edits or _has_image(p):
            continue
        new_text = edits[i]
        if p.runs:
            p.runs[0].text = new_text
            for r in p.runs[1:]:
                r.text = ""
        else:
            p.add_run(new_text)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def save_edited_document(req, new_docx_bytes: bytes, *, employee=None):
    """Пересохранить отредактированный .docx: re-PDF + S3 + подшивка + обновить req."""
    pdf_bytes = docx_to_pdf(new_docx_bytes)
    base = f"Исх {req.outgoing_number} — {req.title}"[:120]
    docx_sf = _store(new_docx_bytes, filename=f"{base}.docx", content_type=DOCX_CT)
    pdf_sf = _store(pdf_bytes, filename=f"{base}.pdf", content_type="application/pdf")
    client = req.case.service.client
    _attach(client, pdf_sf, employee)
    _attach(client, docx_sf, employee)
    req.document_docx = docx_sf
    req.document_pdf = pdf_sf
    req.generated_at = timezone.now()
    req.save(update_fields=["document_docx", "document_pdf", "generated_at", "updated_at"])
    return req


def _store(file_bytes, *, filename, content_type):
    bucket, key = upload_file_to_s3(
        file_bytes, prefix="procedure/requests", filename=filename, content_type=content_type,
    )
    return StoredFile.objects.create(
        bucket=bucket, key=key, filename=filename, content_type=content_type, size=len(file_bytes),
    )


def _attach(client, stored, employee):
    root = get_or_create_root(client)
    folder = _mk(client, root, "Запросы", "requests", 5)
    ClientFile.objects.create(
        folder=folder, stored_file=stored, name=stored.filename,
        size=stored.size or 0, content_type=stored.content_type, uploaded_by=employee,
    )


def generate_request_document(req, *, with_signature=False, marriage_cert="", employee=None):
    """Сформировать документ запроса. Возвращает req (с document_pdf/docx).

    🛑 Наложение подписи/печати (PNG) — TODO, когда заданы signature_file/stamp_file
    у АУ; сейчас `with_signature` только сохраняется.
    """
    rtype = req.request_type
    tpl = rtype.template if rtype else None
    if tpl is None or not tpl.stored_file_id:
        raise RequestDocError(
            "У типа запроса не задан шаблон документа. Привяжите .docx в справочнике «Типы запросов»."
        )
    # Исходящий номер (сквозной по делу) — присваиваем один раз.
    if not req.outgoing_number:
        mx = req.case.requests.aggregate(m=Max("outgoing_number"))["m"] or 0
        req.outgoing_number = mx + 1

    ctx = build_request_context(req, marriage_cert=marriage_cert)
    template_bytes = download_file_from_s3(tpl.stored_file.bucket, tpl.stored_file.key)
    docx_bytes = render_docx(template_bytes, ctx)
    if with_signature:
        proc = _am_procedure(req.case)
        am = proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None
        if am is not None:
            docx_bytes = _apply_signature(docx_bytes, am)
    pdf_bytes = docx_to_pdf(docx_bytes)

    base = f"Исх {req.outgoing_number} — {req.title}"[:120]
    docx_sf = _store(docx_bytes, filename=f"{base}.docx", content_type=DOCX_CT)
    pdf_sf = _store(pdf_bytes, filename=f"{base}.pdf", content_type="application/pdf")

    client = req.case.service.client
    _attach(client, pdf_sf, employee)
    _attach(client, docx_sf, employee)

    req.document_pdf = pdf_sf
    req.document_docx = docx_sf
    req.with_signature = bool(with_signature)
    req.generated_at = timezone.now()
    req.save(update_fields=[
        "outgoing_number", "document_pdf", "document_docx",
        "with_signature", "generated_at", "updated_at",
    ])
    try:
        from apps.crm.models import ActionType
        ActionType.objects.get_or_create(
            code="request_document_created",
            defaults={"name": "Сформирован запрос в госорган", "order": 36, "is_manual": False},
        )
        client_log.invalidate_cache()
        client_log.record_action(
            client, "request_document_created",
            comment=f"Сформирован запрос (исх. № {req.outgoing_number}): "
                    f"{req.title} → {req.recipient_display}. Файл — в папке «Запросы».",
            employee=employee, stored_file=pdf_sf,
        )
    except Exception:
        log.exception("generate_request_document: не удалось записать событийку")
    return req
