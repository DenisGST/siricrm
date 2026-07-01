# -*- coding: utf-8 -*-
"""Генератор почтовых конвертов (Почта России) для печати на принтере.

Макет на ЧИСТЫЙ конверт: отправитель сверху-слева, получатель снизу-справа,
индекс — текстом в адресном блоке. Размер страницы = размер конверта (landscape).

Ядро (этой итерации) — функции, отдающие bytes PDF; UI/хранение — отдельно.
"""
import io
import re

from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

# Переиспользуем регистрацию кириллического шрифта DejaVu из анкет.
from apps.questionnaire.pdf import _ensure_font

# Размеры конвертов (ширина, высота) — длинная сторона горизонтально (landscape).
SIZES = {
    "C5": (229 * mm, 162 * mm),   # A4 сложенный вдвое — основной
    "DL": (220 * mm, 110 * mm),   # E65, A4 втрое — основной
    "C4": (324 * mm, 229 * mm),   # A4 целиком (на будущее)
    "C6": (162 * mm, 114 * mm),   # на будущее
}
DEFAULT_SIZE = "C5"

_INDEX_RE = re.compile(r"\b(\d{6})\b")


def extract_index(text) -> str:
    """Первый 6-значный почтовый индекс из строки (regex \\b\\d{6}\\b)."""
    if not text:
        return ""
    m = _INDEX_RE.search(str(text))
    return m.group(1) if m else ""


def _fonts():
    _ensure_font()
    from reportlab.pdfbase import pdfmetrics
    reg = pdfmetrics.getRegisteredFontNames()
    font = "DejaVu" if "DejaVu" in reg else "Helvetica"
    bold = "DejaVu-Bold" if "DejaVu-Bold" in reg else "Helvetica-Bold"
    return font, bold


# ── сборщики «сторон» (party = {name, address, index}) ──────────────────────
def _party(name="", address="", index=""):
    return {"name": (name or "").strip(),
            "address": (address or "").strip(),
            "index": (index or "").strip()}


def sender_from_executor(executor=None):
    """Отправитель из ExecutorOrg (поле requisites — свободный текст)."""
    from .models import ExecutorOrg
    ex = executor or ExecutorOrg.get_default()
    if ex is None:
        return _party()
    req = (ex.requisites or "").strip()
    lines = [l.strip() for l in req.split("\n") if l.strip()]
    name = lines[0] if lines else (ex.name or "")
    idx = extract_index(req)
    address = ""
    for l in lines[1:]:
        if idx and idx in l:
            address = l
            break
    if not address and len(lines) > 1:
        address = lines[1]
    return _party(name, address, idx)


def party_from_legal_entity(le):
    addr = (le.postal_address or le.legal_address or "").strip()
    return _party(le.name or le.short_name, addr, extract_index(addr))


def party_from_address(addr, name=""):
    """Для crm.Address (клиент/суд) — есть отдельное поле postal_code."""
    if addr is None:
        return _party(name)
    text = (addr.result or addr.source or "").strip()
    idx = (getattr(addr, "postal_code", "") or "").strip() or extract_index(text)
    return _party(name, text, idx)


def party_from_client(client):
    addr = (client.addresses.filter(address_type="postal").first()
            or client.addresses.filter(address_type="actual").first()
            or client.addresses.filter(address_type="registration").first()
            or client.addresses.first())
    fio = " ".join(p for p in [client.last_name, client.first_name,
                               client.patronymic] if p).strip()
    return party_from_address(addr, name=fio)


def recipients_for_case_creditors(service):
    """Список «сторон»-кредиторов дела (для пакетной печати рассылки заявления).

    Каждая сторона дополнена флагом has_address (для отчёта о пропусках реквизитов).
    """
    from apps.crm.models import LegalEntity
    from . import isk_context
    resp = isk_context.latest_response(service)
    at = isk_context.answers_by_type(resp)
    out = []
    for c in isk_context.resolve_creditors(at):
        le = (LegalEntity.objects.filter(pk=c["le_id"]).first()
              if c.get("le_id") else None)
        if le:
            p = party_from_legal_entity(le)
        else:
            p = _party(c.get("name", ""), c.get("address", ""),
                       extract_index(c.get("address", "")))
        p["has_address"] = bool(p["address"])
        out.append(p)
    return out


# ── рендер ──────────────────────────────────────────────────────────────────
def _field(c, x, y, width, label, value, font, bold, fsize, value_bold=False):
    """Печатает «label value» с переносом value по ширине. Возвращает нижний y."""
    leading = fsize + 3
    c.setFont(bold, fsize)
    c.drawString(x, y, label)
    label_w = stringWidth(label, bold, fsize) + 3
    vfont = bold if value_bold else font
    c.setFont(vfont, fsize)
    avail = max(width - label_w, 20 * mm)
    lines = simpleSplit(value or "—", vfont, fsize, avail)
    if not lines:
        lines = ["—"]
    c.drawString(x + label_w, y, lines[0])
    for ln in lines[1:]:
        y -= leading
        c.drawString(x + label_w, y, ln)
    return y - leading


def _draw_envelope(c, W, H, sender, recipient):
    font, bold = _fonts()
    m = 12 * mm

    # Отправитель — верх-слева (≈ левая половина).
    sx, sw = m, W * 0.52
    sy = H - m - 4 * mm
    sf = 9
    sy = _field(c, sx, sy, sw, "От кого:", sender.get("name"), font, bold, sf)
    sy = _field(c, sx, sy, sw, "Откуда:", sender.get("address"), font, bold, sf)
    _field(c, sx, sy, sw, "Индекс:", sender.get("index"), font, bold, sf)

    # Получатель — низ-справа (правая половина, в рамке).
    rx, rw = W * 0.46, W * 0.46
    box_x, box_y = rx - 4 * mm, m
    box_w, box_h = W - box_x - m, H * 0.46
    c.setLineWidth(0.5)
    c.rect(box_x, box_y, box_w, box_h)
    rf = 12 if W > 200 * mm else 10
    rf_name = rf * 0.8  # поля «Кому»/«Куда» — на 20% меньше
    ry = box_y + box_h - rf - 4
    ry = _field(c, rx, ry, rw, "Кому:", recipient.get("name"), font, bold, rf_name,
                value_bold=True)
    _field(c, rx, ry, rw, "Куда:", recipient.get("address"), font, bold, rf_name)
    # Индекс — по низу блока получателя.
    _field(c, rx, box_y + 5 * mm, rw, "Индекс:", recipient.get("index"), font, bold, rf)


def render_envelope(sender, recipient, size=DEFAULT_SIZE) -> bytes:
    W, H = SIZES.get(size, SIZES[DEFAULT_SIZE])
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    _draw_envelope(c, W, H, sender, recipient)
    c.showPage()
    c.save()
    return buf.getvalue()


def render_envelopes(sender, recipients, size=DEFAULT_SIZE) -> bytes:
    """Пакет: по одному конверту на страницу. recipients — список party-dict."""
    W, H = SIZES.get(size, SIZES[DEFAULT_SIZE])
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    for rcp in recipients:
        _draw_envelope(c, W, H, sender, rcp)
        c.showPage()
    c.save()
    return buf.getvalue()
