import io
import logging
from datetime import timezone as tz

import boto3
from botocore.config import Config
from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

log = logging.getLogger(__name__)

# ── Шрифт с поддержкой кириллицы ────────────────────────────────────────────
_FONT_REGISTERED = False

def _ensure_font():
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return
    import os
    # DejaVu поставляется вместе с reportlab
    font_dir = os.path.join(os.path.dirname(__file__), "fonts")
    dejavu = os.path.join(font_dir, "DejaVuSans.ttf")
    dejavu_b = os.path.join(font_dir, "DejaVuSans-Bold.ttf")

    # Fallback: ищем в стандартных местах
    if not os.path.exists(dejavu):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        ]
        for c in candidates:
            if os.path.exists(c):
                dejavu = c
                break

    if not os.path.exists(dejavu_b):
        candidates_b = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        ]
        for c in candidates_b:
            if os.path.exists(c):
                dejavu_b = c
                break

    if os.path.exists(dejavu):
        pdfmetrics.registerFont(TTFont("DejaVu", dejavu))
    if os.path.exists(dejavu_b):
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", dejavu_b))
    _FONT_REGISTERED = True


# ── Стили ────────────────────────────────────────────────────────────────────
def _styles():
    _ensure_font()
    base = getSampleStyleSheet()
    font = "DejaVu" if "DejaVu" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    font_b = "DejaVu-Bold" if "DejaVu-Bold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
    return {
        "title":    ParagraphStyle("title",    fontName=font_b, fontSize=14, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", fontName=font,   fontSize=9,  textColor=colors.HexColor("#64748b"), spaceAfter=12),
        "page":     ParagraphStyle("page",     fontName=font_b, fontSize=11, spaceBefore=14, spaceAfter=4, textColor=colors.HexColor("#1e40af")),
        "question": ParagraphStyle("question", fontName=font,   fontSize=8,  textColor=colors.HexColor("#64748b"), spaceBefore=8, spaceAfter=2),
        "answer":   ParagraphStyle("answer",   fontName=font,   fontSize=9,  spaceBefore=0, spaceAfter=2),
        "answer_b": ParagraphStyle("answer_b", fontName=font_b, fontSize=9,  spaceBefore=0, spaceAfter=2),
        "sub":      ParagraphStyle("sub",      fontName=font,   fontSize=8,  leftIndent=10, textColor=colors.HexColor("#374151")),
        "label":    ParagraphStyle("label",    fontName=font,   fontSize=7,  leftIndent=10, textColor=colors.HexColor("#94a3b8")),
        "font": font, "font_b": font_b,
    }


# ── Рендер ответа в текст/flowables ─────────────────────────────────────────

def _render_answer(q, ans, st):
    """Возвращает список Flowable для одного вопроса."""
    qt = q.question_type
    items = []

    def p(text, style="answer"):
        return Paragraph(str(text) if text else "—", st[style])

    def empty(val):
        return not val or val in ("", [], {})

    if qt in ("text", "textarea", "number", "money"):
        v = ans.get("v") or ""
        items.append(p(v or "—"))

    elif qt == "date":
        v = ans.get("v") or ""
        items.append(p(v or "—"))

    elif qt == "yes_no":
        mapping = {"yes": "Да", "no": "Нет", "unknown": "Не знаю"}
        items.append(p(mapping.get(ans.get("v", ""), "—")))

    elif qt == "full_name_date":
        fio = ans.get("fio") or "—"
        dob = ans.get("dob") or ""
        items.append(p(f"{fio}  {('(' + dob + ')') if dob else ''}"))

    elif qt == "region_ref":
        from apps.crm.models import Region
        ref = ans.get("ref") or ans.get("v") or ""
        try:
            r = Region.objects.get(pk=ref)
            items.append(p(r.name))
        except Exception:
            items.append(p(ref or "—"))

    elif qt == "legal_entity_ref":
        from apps.crm.models import LegalEntity
        ref = ans.get("ref") or ans.get("v") or ""
        text = ans.get("text") or ""
        try:
            le = LegalEntity.objects.get(pk=ref)
            items.append(p(le.name))
        except Exception:
            items.append(p(text or ref or "—"))

    elif qt in ("client_ref", "employee_ref"):
        from apps.crm.models import Client
        from apps.core.models import Employee
        ref = ans.get("ref") or ans.get("v") or ""
        text = ans.get("text") or ""
        label = "—"
        try:
            if qt == "client_ref":
                obj = Client.objects.get(pk=ref)
                label = f"{obj.last_name} {obj.first_name}"
            else:
                obj = Employee.objects.select_related("user").get(pk=ref)
                label = f"{obj.user.last_name} {obj.user.first_name}"
        except Exception:
            label = text or ref or "—"
        items.append(p(label))

    elif qt == "choice":
        v = ans.get("v") or ""
        label = "—"
        for ch in q.choices.all():
            if str(ch.pk) == v:
                label = ch.text
                break
        items.append(p(label))

    elif qt == "multi_choice":
        checked = []
        for ch in q.choices.all():
            if ans.get(f"ch_{ch.pk}") or (isinstance(ans.get("v"), list) and str(ch.pk) in ans.get("v", [])):
                checked.append(ch.text)
        if not checked:
            items.append(p("—"))
        else:
            for c in checked:
                items.append(Paragraph(f"✓ {c}", st["sub"]))
        comment = ans.get("comment") or ""
        if comment:
            items.append(Paragraph(f"Комментарий: {comment}", st["label"]))

    elif qt == "marital_status":
        status = ans.get("status") or ""
        mapping = {
            "never": "Никогда не состоял(а) в браке",
            "married": "Женат / Замужем",
            "divorced": "Разведён(а)",
            "widowed": "Вдовец / Вдова",
        }
        items.append(p(mapping.get(status, status or "—")))
        spouse = ans.get("spouse") or {}
        if spouse:
            items.append(Paragraph("Супруг(а):", st["label"]))
            items.append(Paragraph(f"  {spouse.get('fio') or '—'}  {spouse.get('dob') or ''}", st["sub"]))
        divorces = ans.get("divorces") or []
        for i, d in enumerate(divorces, 1):
            items.append(Paragraph(f"Развод {i}: {d.get('fio') or '—'}  {d.get('dob') or ''}  дата развода: {d.get('divorce_date') or '—'}", st["sub"]))

    elif qt == "bank_debts":
        entries = ans.get("entries") or []
        if not entries:
            items.append(p("—"))
        for i, e in enumerate(entries, 1):
            bank = e.get("bank_name") or e.get("bank_id") or "—"
            items.append(Paragraph(f"Банк {i}: {bank}", st["answer_b"]))
            rows = [
                ("Остаток долга", e.get("balance") or "—"),
                ("Сумма кредита", e.get("loan_amount") or "—"),
                ("Дата взятия", e.get("date_taken") or "—"),
                ("Последний платёж", e.get("last_payment_date") or "—"),
                ("Тип кредита", _LOAN_TYPES.get(e.get("loan_type", ""), e.get("loan_type") or "—")),
                ("Просрочки", "Да" if e.get("overdue") == "yes" else "Нет"),
                ("Решение суда", "Да" if e.get("court_decision") == "yes" else "Нет"),
                ("Исп. производство", "Да" if e.get("enforcement") == "yes" else "Нет"),
                ("Коллекторы", "Да" if e.get("collectors") == "yes" else "Нет"),
            ]
            if e.get("collectors") == "yes" and e.get("collectors_name"):
                rows.append(("Кому передан долг", e["collectors_name"]))
            if e.get("comment"):
                rows.append(("Комментарий", e["comment"]))
            items += _mini_table(rows, st)

    elif qt == "mfo_debts":
        mode = ans.get("mode") or "known"
        if mode == "unknown":
            cnt = ans.get("approx_count") or "—"
            amt = ans.get("approx_amount") or "—"
            items.append(p(f"Не помню точно — примерно {cnt} МФО, общая сумма ~{amt} руб."))
        else:
            entries = ans.get("entries") or []
            if not entries:
                items.append(p("—"))
            for i, e in enumerate(entries, 1):
                mfo = e.get("mfo_name") or "—"
                items.append(Paragraph(f"МФО {i}: {mfo}", st["answer_b"]))
                rows = [
                    ("Остаток долга", e.get("balance") or "—"),
                    ("Дата взятия", e.get("date_taken") or "—"),
                    ("Последний платёж", e.get("last_payment_date") or "—"),
                    ("Просрочки", "Да" if e.get("overdue") == "yes" else "Нет"),
                ]
                items += _mini_table(rows, st)

    elif qt == "property_assets":
        has = ans.get("has_assets") or "no"
        if has != "yes":
            items.append(p("Нет имущества"))
        else:
            entries = ans.get("entries") or []
            if not entries:
                items.append(p("—"))
            for i, e in enumerate(entries, 1):
                atype = _ASSET_TYPES.get(e.get("asset_type", ""), e.get("asset_type") or "—")
                name = e.get("name") or ""
                items.append(Paragraph(f"Имущество {i}: {atype}{' — ' + name if name else ''}", st["answer_b"]))
                rows = [
                    ("Приобретение", _ACQ_TYPES.get(e.get("acquisition", ""), e.get("acquisition") or "—")),
                    ("Стоимость", e.get("value") or "—"),
                    ("В залоге", "Да" if e.get("pledged") == "yes" else "Нет"),
                    ("В браке", "Да" if e.get("in_marriage") == "yes" else "Нет"),
                    ("Позиция по торгам", e.get("auction") or "—"),
                ]
                if e.get("comment"):
                    rows.append(("Комментарий", e["comment"]))
                items += _mini_table(rows, st)

    elif qt == "sold_assets":
        has = ans.get("has_sold") or "no"
        if has != "yes":
            items.append(p("Сделок не было"))
        else:
            entries = ans.get("entries") or []
            if not entries:
                items.append(p("—"))
            for i, e in enumerate(entries, 1):
                atype = _ASSET_TYPES.get(e.get("asset_type", ""), e.get("asset_type") or "—")
                name = e.get("name") or ""
                items.append(Paragraph(f"Сделка {i}: {atype}{' — ' + name if name else ''}", st["answer_b"]))
                rows = [
                    ("Как продано", _SALE_TYPES.get(e.get("sale_type", ""), e.get("sale_type") or "—")),
                    ("Стоимость", e.get("value") or "—"),
                    ("Кому продано", _BUYER_TYPES.get(e.get("buyer_type", ""), e.get("buyer_type") or "—")),
                    ("Документы готовы", "Да" if e.get("has_docs") == "yes" else "Нет"),
                    ("Позиция", _STRATEGY_TYPES.get(e.get("strategy", ""), e.get("strategy") or "—")),
                ]
                if e.get("comment"):
                    rows.append(("Комментарий", e["comment"]))
                items += _mini_table(rows, st)

    elif qt == "utility_debts":
        entries = ans.get("entries") or []
        if not entries:
            items.append(p("—"))
        else:
            rows = [("Организация", "Наименование долга", "Сумма")]
            for e in entries:
                rows.append((e.get("org_name") or "—", e.get("debt_name") or "—", e.get("amount") or "—"))
            items.append(_wide_table(rows, st))

    elif qt == "fine_debts":
        entries = ans.get("entries") or []
        if not entries:
            items.append(p("—"))
        else:
            rows = [("Госорган", "За что штраф", "Сумма")]
            for e in entries:
                rows.append((e.get("agency") or "—", e.get("reason") or "—", e.get("amount") or "—"))
            items.append(_wide_table(rows, st))

    elif qt == "court_debts":
        entries = ans.get("entries") or []
        if not entries:
            items.append(p("—"))
        else:
            rows = [("Суд", "Решение по делу", "Сумма")]
            for e in entries:
                rows.append((e.get("court_name") or "—", e.get("decision") or "—", e.get("amount") or "—"))
            items.append(_wide_table(rows, st))

    elif qt == "other_debts":
        entries = ans.get("entries") or []
        if not entries:
            items.append(p("—"))
        else:
            rows = [("Сущность задолженности", "Сумма")]
            for e in entries:
                rows.append((e.get("essence") or "—", e.get("amount") or "—"))
            items.append(_wide_table(rows, st, col_widths=[12 * cm, 4 * cm]))

    elif qt == "tax_debts":
        has = ans.get("has_debt") or "no"
        if has != "yes":
            items.append(p("Задолженностей по налогам нет"))
        else:
            types = ans.get("types") or []
            tax_labels = {
                "commercial": "Коммерческие (закрытие ИП)",
                "property_realty": "Имущественные — недвижимость",
                "property_car": "Имущественные — автомобиль",
                "ndfl": "Налог на доходы (НДФЛ)",
                "other": "Иные налоги",
            }
            for k in ["commercial", "property_realty", "property_car", "ndfl"]:
                if k in types:
                    amount = ans.get(f"a_{k}") or "—"
                    items.append(Paragraph(f"✓ {tax_labels[k]}: {amount} руб.", st["sub"]))
            if "other" in types:
                oname = ans.get("other_name") or "—"
                oamt = ans.get("other_amount") or "—"
                items.append(Paragraph(f"✓ Иные налоги: {oname} — {oamt} руб.", st["sub"]))

    elif qt == "children_list":
        has = ans.get("has_children") or "no"
        if has != "yes":
            items.append(p("Нет несовершеннолетних детей на иждивении"))
        else:
            entries = ans.get("entries") or []
            if not entries:
                items.append(p("—"))
            for i, e in enumerate(entries, 1):
                fio = e.get("fio") or "—"
                dob = e.get("dob") or ""
                items.append(Paragraph(f"Ребёнок {i}: {fio}{' (' + dob + ')' if dob else ''}", st["sub"]))

    else:
        v = ans.get("v") or ""
        items.append(p(v or "—"))

    return items or [p("—")]


# ── Справочники для читаемого рендера ────────────────────────────────────────
_LOAN_TYPES = {"non_target": "Нецелевой", "target": "Целевой", "secured": "Залоговый", "mortgage": "Ипотека"}
_ASSET_TYPES = {
    "residential": "Жилая недвижимость", "commercial": "Коммерческая недвижимость",
    "garage": "Гараж", "dacha": "Дача", "car": "Автомобиль",
    "special": "Спецтехника", "boat": "Лодка",
    "claim_right": "Право требования", "money": "Деньги", "other": "Иное",
}
_ACQ_TYPES = {"purchase": "Купля-продажа", "gift": "Дарение", "inheritance": "Наследство", "other": "Иное"}
_SALE_TYPES = {"purchase": "Купля-продажа", "gift": "Дарение", "other": "Иное"}
_BUYER_TYPES = {"relative": "Родственнику", "unknown": "Неизвестному человеку", "legal": "Юридическому лицу"}
_STRATEGY_TYPES = {"normal": "Признаём нормальной", "challenge": "Оспариваем", "other": "Иное"}


# ── Вспомогательные таблицы ──────────────────────────────────────────────────
def _mini_table(rows, st):
    """Двухколоночная таблица label-value."""
    font = st["font"]
    font_b = st["font_b"]
    table_data = []
    for label, val in rows:
        table_data.append([
            Paragraph(str(label), ParagraphStyle("tl", fontName=font, fontSize=7, textColor=colors.HexColor("#94a3b8"))),
            Paragraph(str(val),   ParagraphStyle("tv", fontName=font, fontSize=8)),
        ])
    t = Table(table_data, colWidths=[5 * cm, 11 * cm], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",  (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    return [t]


def _wide_table(rows, st, col_widths=None):
    """Таблица с заголовком для однотипных записей."""
    font = st["font"]
    font_b = st["font_b"]
    if col_widths is None:
        n = len(rows[0])
        w = 16 * cm / n
        col_widths = [w] * n
    table_data = []
    for i, row in enumerate(rows):
        is_header = (i == 0)
        f = font_b if is_header else font
        color = colors.HexColor("#1e293b") if is_header else colors.black
        table_data.append([
            Paragraph(str(cell), ParagraphStyle(f"tc{i}", fontName=f, fontSize=8, textColor=color))
            for cell in row
        ])
    t = Table(table_data, colWidths=col_widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    return t


# ── Основная функция генерации ───────────────────────────────────────────────
def generate_response_pdf(response) -> bytes:
    """Генерирует PDF анкеты и возвращает bytes."""
    from apps.questionnaire.models import Answer

    answers = {str(a.question_id): a.value for a in Answer.objects.filter(response=response)}
    pages = list(response.template.pages.prefetch_related(
        "questions__choices"
    ).order_by("order"))

    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )

    story = []

    # Шапка
    client = response.service.client
    client_name = f"{client.last_name} {client.first_name}"
    if hasattr(client, "middle_name") and client.middle_name:
        client_name += f" {client.middle_name}"
    filled_at = (response.updated_at or response.created_at).astimezone(
        tz.utc
    ).strftime("%d.%m.%Y %H:%M")

    story.append(Paragraph(response.template.title, st["title"]))
    story.append(Paragraph(
        f"Клиент: {client_name}  ·  Договор: {response.service.numb_dogovor or '—'}  ·  Дата: {filled_at}",
        st["subtitle"],
    ))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e2e8f0"), spaceAfter=8))

    for page in pages:
        questions = [q for q in page.questions.all() if q.parent_group_id is None]
        if not questions:
            continue

        if page.title:
            story.append(Paragraph(page.title, st["page"]))

        for q in questions:
            ans = answers.get(str(q.pk)) or {}
            story.append(Paragraph(q.text, st["question"]))
            story += _render_answer(q, ans, st)

        story.append(Spacer(1, 6))

    # Подпись
    story.append(Spacer(1, 24))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cbd5e1"), spaceAfter=10))
    font = st["font"]
    font_b = st["font_b"]
    sig_style = ParagraphStyle("sig", fontName=font, fontSize=9, textColor=colors.HexColor("#1e293b"))
    sig_label = ParagraphStyle("siglbl", fontName=font, fontSize=7, textColor=colors.HexColor("#94a3b8"))
    story.append(Paragraph("Полноту и правильность анкетных данных подтверждаю", sig_style))
    story.append(Spacer(1, 28))
    sig_table = Table(
        [[
            Paragraph("________________________________", sig_style),
            Paragraph("________________________________", sig_style),
        ]],
        colWidths=[9 * cm, 7 * cm],
        hAlign="LEFT",
    )
    story.append(sig_table)
    lbl_table = Table(
        [[
            Paragraph("подпись", sig_label),
            Paragraph("дата", sig_label),
        ]],
        colWidths=[9 * cm, 7 * cm],
        hAlign="LEFT",
    )
    story.append(lbl_table)

    doc.build(story)
    return buf.getvalue()


# ── S3 ───────────────────────────────────────────────────────────────────────
def _s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.AWS_S3_BASE_URL,
        config=Config(
            signature_version="s3v4",
            s3={"payload_signing_enabled": False, "addressing_style": "path"},
        ),
    )


def upload_pdf_to_s3(pdf_bytes: bytes, response_pk: str) -> str:
    """Загружает PDF в S3, возвращает S3-ключ."""
    key = f"questionnaires/{response_pk}/anketa.pdf"
    _s3_client().upload_fileobj(
        io.BytesIO(pdf_bytes),
        settings.AWS_STORAGE_BUCKET_NAME,
        key,
        ExtraArgs={
            "ContentType": "application/pdf",
            "ContentDisposition": 'attachment; filename="anketa.pdf"',
        },
    )
    return key


def get_presigned_url(s3_key: str, expires: int = 3600) -> str:
    """Возвращает presigned URL для скачивания."""
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=expires,
    )


def generate_and_upload(response) -> None:
    """Генерирует PDF и сохраняет в S3. Обновляет поля модели."""
    pdf_bytes = generate_response_pdf(response)
    key = upload_pdf_to_s3(pdf_bytes, str(response.pk))
    response.pdf_s3_key = key
    response.pdf_generated_at = timezone.now()
    response.save(update_fields=["pdf_s3_key", "pdf_generated_at"])


from celery import shared_task  # noqa: E402

@shared_task(name="questionnaire.upload_pdf", ignore_result=True, max_retries=3, default_retry_delay=60)
def upload_pdf_async(response_pk: str):
    """Celery task: генерирует PDF и сохраняет в S3 фоново."""
    from apps.questionnaire.models import QuestionnaireResponse
    try:
        response = QuestionnaireResponse.objects.get(pk=response_pk)
        generate_and_upload(response)
    except Exception as exc:
        log.exception("upload_pdf_async failed for %s: %s", response_pk, exc)
        raise upload_pdf_async.retry(exc=exc)
