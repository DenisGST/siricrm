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


def _esc(v):
    """Экранирование для reportlab Paragraph (XML)."""
    from xml.sax.saxutils import escape
    return escape(str(v or ""))


def consent_pdf(ctx) -> bytes:
    """Заявление о согласии на обработку персональных данных (приложение к договору).

    ctx — контекст договора (contract_bfl.build_context): данные клиента +
    реквизиты Исполнителя из ExecutorOrg ({Реквизиты_исполнителя}, {ispolnitel}).
    """
    from reportlab.lib.enums import TA_JUSTIFY as J
    _ensure_font()
    font = "DejaVu" if "DejaVu" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    font_b = "DejaVu-Bold" if "DejaVu-Bold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
    st_head = ParagraphStyle("head", fontName=font, fontSize=10, leading=13, spaceAfter=2)
    st_from = ParagraphStyle("from", fontName=font, fontSize=10, leading=13, spaceBefore=6, spaceAfter=8)
    st_title = ParagraphStyle("title", fontName=font_b, fontSize=12, alignment=1, spaceAfter=10)
    st_body = ParagraphStyle("body", fontName=font, fontSize=10, leading=14,
                             alignment=J, firstLineIndent=0.8 * cm, spaceAfter=6)
    st_sign = ParagraphStyle("sign", fontName=font, fontSize=10, leading=16, spaceBefore=14)

    last = ctx.get("Фамилия", "")
    first = ctx.get("Имя", "")
    patr = ctx.get("Отчество", "")
    fio = " ".join(p for p in [last, first, patr] if p).strip()
    initials = (last + " " + "".join(f"{p[0]}." for p in [first, patr] if p)).strip()

    requisites = ctx.get("Реквизиты_исполнителя", "") or ""
    ispolnitel = ctx.get("ispolnitel", "") or "Исполнителю"

    pass_line = (f"паспорт {_esc(ctx.get('паспорт_серия'))} номер {_esc(ctx.get('паспорт_номер'))}), "
                 f"выдан {_esc(ctx.get('паспорт_выдан_где'))} {_esc(ctx.get('паспорт_выдан_когда'))} г., "
                 f"адрес регистрации: {_esc(ctx.get('адрес_регистрации'))}, "
                 f"тел. {_esc(ctx.get('номер_телефона'))}")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=2 * cm, leftMargin=2.5 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm)
    story = []
    for line in requisites.split("\n"):
        story.append(Paragraph(_esc(line) or "&nbsp;", st_head))
    story.append(Paragraph(f"От {_esc(fio)}", st_from))
    story.append(Paragraph("Заявление о согласии на обработку персональных данных", st_title))

    story.append(Paragraph(
        f"Я, {_esc(fio)}, {_esc(ctx.get('дата рождения'))} г/р ({pass_line}, "
        f"даю своё согласие {_esc(ispolnitel)} (далее — Исполнитель) на сбор, запись, "
        "систематизацию, накопление, хранение, уточнение (обновление, изменение), "
        "извлечение, использование, обезличивание, блокирование, удаление и уничтожение, "
        "в том числе автоматизированные, своих персональных данных в специализированной "
        "электронной базе данных о моих фамилии, имени, отчестве, дате и месте рождения, "
        "адресе, семейном, социальном, имущественном положении, образовании, профессии, "
        "доходах, месте работы, а также иной информации личного характера, которая может "
        "быть использована при предоставлении Исполнителем консультационных услуг, и в "
        "целях участия в опросах/анкетировании, проводимых Исполнителем для изучения и "
        "исследования мнения клиентов о качестве обслуживания и услугах Исполнителя, при "
        "условии гарантии неразглашения данной информации третьим лицам.", st_body))
    story.append(Paragraph(
        "Я согласен на размещение текстов судебных решений и определений, количества и "
        "наименования кредиторов с указанием суммы задолженности, сроков выполнения работ, "
        "отзывов (текстовых и/или фото- и видеоматериалов), а также в маркетинговых "
        "(рекламных) материалах Исполнителя.", st_body))
    story.append(Paragraph(
        "Я согласен на предоставление мне информации путём направления почтовой "
        "корреспонденции по моему домашнему адресу, посредством электронной почты, "
        "телефонных обращений, смс-сообщений.", st_body))
    story.append(Paragraph(
        "Данное согласие действует с момента подписания настоящего заявления в течение "
        "срока предоставления Исполнителем услуг и пяти лет после прекращения указанных "
        "услуг. По истечении указанного срока действие настоящего заявления считается "
        "продлённым на каждые последующие пять лет при отсутствии у Исполнителя сведений "
        "о его отзыве.", st_body))
    story.append(Paragraph(
        "Данное согласие может быть отозвано путём представления Исполнителю письменного "
        "заявления.", st_body))
    story.append(Paragraph(
        f"«___» __________ 20__ г.&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        f"_______________ /{_esc(initials)}", st_sign))

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
