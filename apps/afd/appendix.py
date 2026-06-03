"""Приложения к договору: график платежей (reportlab) и анкета (PDF)."""
import io
import logging

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

# Переиспользуем регистрацию кириллического шрифта из анкет.
from apps.questionnaire.pdf import _ensure_font

log = logging.getLogger(__name__)

_RU_STATUS = {"scheduled": "запланирован", "overdue": "просрочен", "paid": "оплачен"}


def _fmt_money(value):
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " руб."


def _fmt_date(d):
    return d.strftime("%d.%m.%Y") if d else "—"


def _styles():
    _ensure_font()
    base = getSampleStyleSheet()  # noqa: F841 (нужен для инициализации движка)
    font = "DejaVu" if "DejaVu" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    font_b = "DejaVu-Bold" if "DejaVu-Bold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
    return {
        "title": ParagraphStyle("title", fontName=font_b, fontSize=13, spaceAfter=4, alignment=1),
        "sub": ParagraphStyle("sub", fontName=font, fontSize=9, textColor=colors.HexColor("#64748b"), spaceAfter=10, alignment=1),
        "cell": ParagraphStyle("cell", fontName=font, fontSize=9),
        "cell_b": ParagraphStyle("cell_b", fontName=font_b, fontSize=9),
        "font": font, "font_b": font_b,
    }


def schedule_appendix_pdf(service, *, appendix_no=1) -> bytes | None:
    """График платежей как отдельное приложение к договору. None — если нет начислений."""
    charges = list(service.charges.order_by("due_date"))
    if not charges:
        return None

    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
    )
    story = []
    numb = service.numb_dogovor or "—"
    story.append(Paragraph(f"Приложение №{appendix_no} к Договору № {numb}", st["title"]))
    story.append(Paragraph("График платежей", st["title"]))
    client = service.client
    client_name = f"{client.last_name} {client.first_name} {client.patronymic}".strip()
    story.append(Paragraph(f"Заказчик: {client_name}", st["sub"]))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e2e8f0"), spaceAfter=8))

    header = [
        Paragraph("№", st["cell_b"]), Paragraph("Наименование", st["cell_b"]),
        Paragraph("Срок оплаты", st["cell_b"]), Paragraph("Сумма", st["cell_b"]),
        Paragraph("Статус", st["cell_b"]),
    ]
    rows = [header]
    total = 0
    for i, ch in enumerate(charges, 1):
        total += ch.amount or 0
        rows.append([
            Paragraph(str(i), st["cell"]),
            Paragraph(ch.title or "", st["cell"]),
            Paragraph(_fmt_date(ch.due_date), st["cell"]),
            Paragraph(_fmt_money(ch.amount), st["cell"]),
            Paragraph(_RU_STATUS.get(ch.status, ch.status or ""), st["cell"]),
        ])
    rows.append([
        Paragraph("", st["cell"]), Paragraph("ИТОГО", st["cell_b"]),
        Paragraph("", st["cell"]), Paragraph(_fmt_money(total), st["cell_b"]),
        Paragraph("", st["cell"]),
    ])

    table = Table(rows, colWidths=[1 * cm, 7.5 * cm, 2.8 * cm, 3.2 * cm, 2.5 * cm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f8fafc")),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph(
        "Оплата осуществляется на расчётный счёт Исполнителя либо наличными в кассу Исполнителя.",
        st["cell"],
    ))

    doc.build(story)
    return buf.getvalue()


def questionnaire_appendix_pdf(service) -> bytes | None:
    """PDF анкеты (последний ответ по услуге) как приложение. None — если анкет нет."""
    response = (
        service.questionnaire_responses.order_by("-is_complete", "-updated_at").first()
    )
    if response is None:
        return None
    try:
        from apps.questionnaire.pdf import generate_response_pdf
        return generate_response_pdf(response)
    except Exception:
        log.exception("questionnaire_appendix_pdf: не удалось сгенерировать PDF анкеты")
        return None
