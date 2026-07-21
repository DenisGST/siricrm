# -*- coding: utf-8 -*-
"""Выгрузка отправлений в файл загрузки ЛК «Отправка» Почты России (xlsx).

Этап 1 интеграции с Почтой РФ: юрист отмечает чекбоксами адресатов на вкладке
«Корреспонденция» → получает .xlsx строго в формате официального шаблона Почты →
загружает его в otpravka.pochta.ru, там же печатает конверты со ШПИ и форму 103.

🛑 Файл собирается ПОВЕРХ официального шаблона Почты
`reference_data/pochta_upload_template.xlsx` (3 листа: «Лист» + «Инструкция» +
«Раскр. списки» со списками валидации). Собранный «с нуля» одинокий лист «Лист»
ЛК отбивал «Неверный формат файла» — строгий разбор требует полную структуру
шаблона (проверено 21.07.2026). Порядок/имена колонок (COLUMNS) — из шапки
шаблона, не переименовывать и не переставлять: ЛК разбирает файл по позициям.

Заполняем только то, что осмысленно для заказного письма; остальное остаётся
пустым и берётся из настроек ЛК (так же, как в рабочем образце юриста).
"""
from __future__ import annotations

import io
import os
import re

from apps.afd.envelope import (
    extract_index, party_from_client, party_from_legal_entity, party_from_request,
    recipients_for_case_creditors,
)

# ── формат файла Почты ──────────────────────────────────────────────────────
COLUMNS = [
    "ADDRESSLINE", "ADRESAT", "MASS", "VALUE", "PAYMENT", "COMMENT", "ORDERNUM",
    "TELADDRESS", "MAILTYPE", "MAILCATEGORY", "INDEXFROM", "VLENGTH", "VWIDTH",
    "VHEIGHT", "VOLUME", "FRAGILE", "ENVELOPETYPE", "NOTIFICATIONTYPE", "COURIER",
    "SMSNOTICERECIPIENT", "WOMAILRANK", "PAYMENTMETHOD", "NOTICEPAYMENTMETHOD",
    "COMPLETENESSCHECKING", "NORETURN", "VSD", "TRANSPORTMODE", "EASYRETURN",
    "BRANCHNAME", "GROUPREFERENCE ", "ID_PO", "PREPOSTALPREPARATION",
    "DELIVERYPOINT", "DIMENSIONTYPE", "SHELFLIFEDAYS", "WITHOUTOPENING",
    "CONTENTSCHECKING", "SENDERCOMMENT", "TRANSPORTTYPE", "FARMA",
    "PREPAYMENT_RETURN",
]
SHEET_NAME = "Лист"

MAILTYPE_LETTER = 2          # «письмо» (Корреспонденция)
MAILCATEGORY_ORDERED = 1     # «Письмо заказное»
NOTIFICATION_SIMPLE = "S"    # простое уведомление о вручении (ф.119)
NOTICE_PAYMENT_CASHLESS = "C"  # оплата уведомления — безналичная

# 🛑 Лимиты длины полей Почты (из подсказок шаблона). Превышение → ЛК отбивает
# весь файл «Некоторые значения полей превышают максимально разрешенную длину».
MAX_ADRESAT = 200            # наименование получателя
MAX_ADDRESSLINE = 200        # адрес
MAX_COMMENT = 200            # комментарий к отправлению

# ── вес ─────────────────────────────────────────────────────────────────────
ENVELOPE_WEIGHT_G = 10       # конверт C5
PAGE_WEIGHT_G = 5            # лист А4 80 г/м²
DEFAULT_PAGES = 2            # если число страниц неизвестно
LETTER_MAX_WEIGHT_G = 100    # предел для «письма»; выше — уже бандероль


def letter_weight_g(pages: int | None) -> int:
    """Вес письма в граммах: конверт + листы."""
    n = pages if pages and pages > 0 else DEFAULT_PAGES
    return ENVELOPE_WEIGHT_G + PAGE_WEIGHT_G * n


def weight_kg(grams: int) -> float:
    """Почта ждёт вес в килограммах."""
    return round(grams / 1000, 3)


# ── чистка текста ───────────────────────────────────────────────────────────
_WS_RE = re.compile(r"\s+")


def clean(text) -> str:
    """Убирает неразрывные пробелы и схлопывает пробелы.

    В рабочем образце юриста половина строк начиналась с \\xa0 (следы копипаста
    из документов) — Почта такие строки разбирает хуже.
    """
    if not text:
        return ""
    return _WS_RE.sub(" ", str(text).replace("\xa0", " ")).strip()


def address_line(party: dict) -> str:
    """ADDRESSLINE = индекс + адрес.

    Почта в подсказке к колонке пишет «индекс не указывать, подберётся
    автоматически», но в рабочем образце юриста индекс присутствует и файл
    загружается — оставляем его: у части наших госорганов (МРЭО) адрес
    слишком общий, и без индекса Почта промахивается с отделением.
    """
    addr = clean(party.get("address"))
    idx = clean(party.get("index")) or extract_index(addr)
    if not addr:
        return ""
    if idx and not addr.startswith(idx):
        return f"{idx}, {addr}"
    return addr


# ── сбор адресатов ──────────────────────────────────────────────────────────
def fit_name(party: dict) -> str:
    """Наименование получателя в пределах лимита Почты (ADRESAT ≤200).

    Полные официальные названия госорганов бывают длиннее 200 символов — тогда
    берём короткое наименование (`ФКУ "Центр ГИМС МЧС…"`); если и оно длинное —
    как крайняя мера режем по границе слова (лучше, чем отбитый Почтой файл).
    """
    name = clean(party.get("name"))
    if len(name) <= MAX_ADRESAT:
        return name
    short = clean(party.get("short_name"))
    if short and len(short) <= MAX_ADRESAT:
        return short
    cut = (short or name)[:MAX_ADRESAT]
    return cut[:cut.rfind(" ")].rstrip() if " " in cut[150:] else cut.rstrip()


def _item(party: dict, *, kind: str, key: str, ordernum: str = "",
          pages: int | None = None, comment: str = "") -> dict:
    """Единая строка-кандидат на отправку (до раскладки по колонкам).

    `key` — стабильный идентификатор строки: по нему из формы приходят ручной вес
    и отметка «с уведомлением» именно для этого письма.
    """
    grams = letter_weight_g(pages)
    return {
        "kind": kind,                      # request | creditor | debtor
        "key": key,
        "name": fit_name(party),
        "address": address_line(party),
        "index": clean(party.get("index")),
        "ordernum": ordernum,
        "pages": pages,
        "comment": clean(comment)[:MAX_COMMENT],
        "weight_g": grams,
        "weight_kg": weight_kg(grams),
        "weight_manual": False,            # вес введён юристом, а не посчитан
        "notify": False,                   # уведомление о вручении для этого письма
    }


def items_for_requests(requests) -> list[dict]:
    """Адресаты по выбранным запросам в госорганы."""
    out = []
    for req in requests:
        party = party_from_request(req)
        num = req.outgoing_number
        out.append(_item(
            party, kind="request", key=f"req:{req.pk}",
            ordernum=(f"Исх-{num}" if num else ""),
            pages=req.pages_count,
            comment=req.title,
        ))
    return out


def items_for_creditors(service) -> list[dict]:
    """Адресаты-кредиторы дела (банки/МФО из анкеты БФЛ)."""
    return [
        _item(p, kind="creditor", key=f"cred:{i}")
        for i, p in enumerate(recipients_for_case_creditors(service))
    ]


def item_for_debtor(service) -> dict | None:
    """Сам должник — ему АУ тоже направляет корреспонденцию."""
    client = service.client
    party = party_from_client(client)
    if not party.get("name"):
        return None
    return _item(party, kind="debtor", key="debtor")


def apply_overrides(items: list[dict], *, mass_overrides: dict, notify_keys) -> list[dict]:
    """Накладывает построчные правки юриста: ручной вес (в граммах) и уведомление."""
    notify_keys = set(notify_keys or ())
    for it in items:
        grams = mass_overrides.get(it["key"])
        if grams:
            it["weight_g"] = grams
            it["weight_kg"] = weight_kg(grams)
            it["weight_manual"] = True
        it["notify"] = it["key"] in notify_keys
    return items


def split_ready(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Делит на пригодные к выгрузке и проблемные.

    Проблемные (нет имени / адреса / индекса, либо перевес) в файл не попадают —
    Почта такую строку всё равно забракует при загрузке. Каждой строке проставляет
    `problems` (список или None), чтобы модалка могла показать единый список.
    """
    ready, skipped = [], []
    for it in items:
        problems = []
        if not it["name"]:
            problems.append("нет наименования")
        if not it["address"]:
            problems.append("нет адреса")
        elif not (it["index"] or extract_index(it["address"])):
            problems.append("нет индекса")
        elif len(it["address"]) > MAX_ADDRESSLINE:
            # Адрес резать нельзя (сломает доставку) — отдаём юристу на правку.
            problems.append(
                f"адрес длиннее {MAX_ADDRESSLINE} символов — сократите его"
            )
        if it["weight_g"] > LETTER_MAX_WEIGHT_G:
            problems.append(
                f"перевес {it['weight_g']} г при пределе {LETTER_MAX_WEIGHT_G} г — "
                f"это уже бандероль, не письмо (поправьте вес)"
            )
        it["problems"] = problems or None
        (skipped if problems else ready).append(it)
    return ready, skipped


# ── сборка книги ────────────────────────────────────────────────────────────
def build_rows(items: list[dict], *, index_from: str = "") -> list[dict]:
    """Раскладывает адресатов по колонкам шаблона Почты.

    Уведомление о вручении — построчно (`item["notify"]`): в одной партии бывают
    и обычные заказные, и заказные с уведомлением.
    """
    rows = []
    for it in items:
        row = {c: None for c in COLUMNS}
        row["ADDRESSLINE"] = it["address"]
        row["ADRESAT"] = it["name"]
        row["MASS"] = it["weight_kg"]
        row["MAILTYPE"] = MAILTYPE_LETTER
        row["MAILCATEGORY"] = MAILCATEGORY_ORDERED
        if it["ordernum"]:
            row["ORDERNUM"] = it["ordernum"]
        if it["comment"]:
            row["COMMENT"] = it["comment"]
        if index_from:
            row["INDEXFROM"] = index_from
        if it.get("notify"):
            row["NOTIFICATIONTYPE"] = NOTIFICATION_SIMPLE
            row["NOTICEPAYMENTMETHOD"] = NOTICE_PAYMENT_CASHLESS
        # ENVELOPETYPE / PAYMENTMETHOD намеренно пустые — берутся из настроек ЛК.
        rows.append(row)
    return rows


TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "reference_data", "pochta_upload_template.xlsx",
)


def build_workbook(rows: list[dict]) -> bytes:
    """xlsx поверх официального шаблона Почты (3 листа, валидации, шапка).

    Строки данных дописываются в лист «Лист» под шапкой. 🛑 Дописываем ПО ПОЗИЦИЯМ
    колонок (не по имени) — шапка шаблона и есть источник COLUMNS.
    """
    import openpyxl

    wb = openpyxl.load_workbook(TEMPLATE_PATH)
    ws = wb[SHEET_NAME]
    for row in rows:
        ws.append([row.get(c) for c in COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def index_from_for_case(case) -> str:
    """INDEXFROM — индекс ОПС, через которое сдаёт почту ФУ этого дела."""
    from .request_documents import _am_procedure

    proc = _am_procedure(case)
    am = proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None
    if am is None:
        return ""
    return clean(am.ops_index) or extract_index(am.corr_address)


def collect_items(case, *, requests=(), with_creditors=False, with_debtor=False,
                  mass_overrides=None, notify_keys=()):
    """Собирает всех адресатов дела с наложенными построчными правками."""
    service = case.service
    items = list(items_for_requests(requests))
    if with_creditors:
        items += items_for_creditors(service)
    if with_debtor:
        debtor = item_for_debtor(service)
        if debtor:
            items.append(debtor)
    return apply_overrides(
        items, mass_overrides=(mass_overrides or {}), notify_keys=notify_keys,
    )


def export_case(case, *, requests=(), with_creditors=False, with_debtor=False,
                mass_overrides=None, notify_keys=()):
    """Главная точка: собирает адресатов и отдаёт (xlsx_bytes, ready, skipped).

    xlsx_bytes = None, если после отсева не осталось ни одной пригодной строки.
    """
    items = collect_items(
        case, requests=requests, with_creditors=with_creditors,
        with_debtor=with_debtor, mass_overrides=mass_overrides,
        notify_keys=notify_keys,
    )
    ready, skipped = split_ready(items)
    if not ready:
        return None, ready, skipped
    rows = build_rows(ready, index_from=index_from_for_case(case))
    return build_workbook(rows), ready, skipped
