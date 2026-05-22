"""Парсинг и нормализация полей Bubble.

Особенности данных Bubble (выяснены при сверке структуры):
* числовые поля могут приходить и числом, и строкой (inn/INN, PaspSer/PassSer);
* даты — ISO-8601 с Z, но dateTimeInMessage в MessageWSP битое — не использовать;
* телефоны — 10 цифр без префикса страны;
* Bubble отдаёт только непустые поля объекта.
"""
import datetime
import re
from decimal import Decimal, InvalidOperation

from django.utils.dateparse import parse_datetime, parse_date


def clean_str(v) -> str:
    """Любое значение → аккуратная строка."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def first_nonempty(*values) -> str:
    """Первое непустое значение как строка (для дублирующихся полей inn/INN)."""
    for v in values:
        s = clean_str(v)
        if s:
            return s
    return ""


def parse_bubble_dt(v):
    """ISO-строка Bubble → aware datetime или None."""
    s = clean_str(v)
    if not s:
        return None
    dt = parse_datetime(s)
    return dt


def parse_bubble_date(v):
    """ISO-строка Bubble → date или None."""
    s = clean_str(v)
    if not s:
        return None
    dt = parse_datetime(s)
    if dt:
        return dt.date()
    return parse_date(s)


def normalize_phone(v) -> str:
    """Любой телефон → 11 цифр в формате 7XXXXXXXXXX (E.164 без +).

    Возвращает '' если распознать не удалось.
    """
    digits = re.sub(r"\D", "", clean_str(v))
    if not digits:
        return ""
    if len(digits) == 10:                 # 9XXXXXXXXX → 79XXXXXXXXX
        return "7" + digits
    if len(digits) == 11 and digits[0] == "8":
        return "7" + digits[1:]
    if len(digits) == 11 and digits[0] == "7":
        return digits
    return ""                              # нестандартное — пусть оператор поправит


def parse_decimal(v) -> Decimal:
    """Число Bubble → Decimal. '' / мусор → Decimal('0')."""
    s = clean_str(v).replace(",", ".").replace(" ", "")
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def parse_int(v, default=0) -> int:
    s = clean_str(v)
    if not s:
        return default
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def gender_from_bubble(v) -> str:
    """Поле «Пол» Bubble → код Client.gender."""
    s = clean_str(v).lower()
    if s.startswith("жен"):
        return "female"
    if s.startswith("муж"):
        return "male"
    return ""


# ─── Извлечение display-полей для UI staging-таблицы ──────────

def man_display(raw: dict) -> dict:
    """Из сырого объекта Man вытащить поля для таблицы аудита."""
    last = clean_str(raw.get("lName"))
    first = clean_str(raw.get("fName"))
    patr = clean_str(raw.get("mName"))
    fio = " ".join(p for p in (last, first, patr) if p) or "(без ФИО)"

    phone = normalize_phone(raw.get("tel"))
    email = clean_str(raw.get("email"))
    city = clean_str(raw.get("city")) or clean_str(raw.get("cityR"))
    subtitle_parts = []
    if phone:
        subtitle_parts.append("+" + phone)
    if email:
        subtitle_parts.append(email)
    if city:
        subtitle_parts.append(city)

    return {
        "display_title": fio[:300],
        "display_subtitle": " · ".join(subtitle_parts)[:300],
        "bubble_created": parse_bubble_dt(raw.get("Created Date")),
    }


def projectbfl_display(raw: dict) -> dict:
    """Из сырого ProjectBFL — поля для таблицы аудита услуг."""
    fio = clean_str(raw.get("namePrj")) or "(услуга без ФИО)"
    numb = clean_str(raw.get("numbDogovor"))
    summa = clean_str(raw.get("SummaDogovor"))
    date_dog = parse_bubble_date(raw.get("DateDogovor"))
    parts = []
    if numb:
        parts.append(f"договор №{numb}")
    if date_dog:
        parts.append(date_dog.strftime("%d.%m.%Y"))
    if summa:
        parts.append(f"{summa} ₽")
    return {
        "display_title": fio[:300],
        "display_subtitle": " · ".join(parts)[:300],
        "bubble_created": parse_bubble_dt(raw.get("Created Date")),
    }


def money_kind(raw: dict) -> str:
    """Тип записи Money: accrual / debit / credit / empty."""
    if parse_decimal(raw.get("accrual")) > 0:
        return "accrual"
    if parse_decimal(raw.get("debit")) > 0:
        return "debit"
    if parse_decimal(raw.get("credit")) > 0:
        return "credit"
    return "empty"


def money_display(raw: dict) -> dict:
    """Из сырого Money — поля для таблицы аудита платежей."""
    kind = money_kind(raw)
    kind_ru = {
        "accrual": "Начисление", "debit": "Входящий",
        "credit": "Исходящий", "empty": "—",
    }[kind]
    amount = parse_decimal(raw.get(kind)) if kind != "empty" else 0
    date = parse_bubble_date(raw.get("date"))
    parts = [kind_ru]
    if amount:
        parts.append(f"{amount} ₽")
    if date:
        parts.append(date.strftime("%d.%m.%Y"))
    return {
        "display_title": (clean_str(raw.get("name")) or "(без названия)")[:300],
        "display_subtitle": " · ".join(parts)[:300],
        "bubble_created": parse_bubble_dt(raw.get("Created Date")),
    }


def messagewsp_display(raw: dict) -> dict:
    """Из сырого MessageWSP — поля для таблицы аудита сообщений."""
    name = clean_str(raw.get("senderName")) or clean_str(raw.get("chatName"))
    phone = normalize_phone(raw.get("NumberTel"))
    title = name or ("+" + phone if phone else "(без имени)")
    from_me = bool(raw.get("fromMe"))
    mtype = clean_str(raw.get("type")) or "?"
    body = clean_str(raw.get("body")) or clean_str(raw.get("caption"))
    parts = ["исходящее" if from_me else "входящее", mtype]
    if body:
        parts.append(body[:60])
    return {
        "display_title": title[:300],
        "display_subtitle": " · ".join(parts)[:300],
        "bubble_created": parse_bubble_dt(raw.get("Created Date")),
    }


def files_display(raw: dict) -> dict:
    """Из сырого Files — поля для таблицы аудита файлов."""
    fname = clean_str(raw.get("filename")) or "(без имени)"
    directory = clean_str(raw.get("directory"))
    link = clean_str(raw.get("linkGDrive"))
    storage = ""
    if "google" in link:
        storage = "Google Drive"
    elif "amazonaws" in link or "appforest" in link:
        storage = "Bubble S3"
    parts = [p for p in (directory, storage) if p]
    return {
        "display_title": fname[:300],
        "display_subtitle": " · ".join(parts)[:300],
        "bubble_created": parse_bubble_dt(raw.get("Created Date")),
    }


# Реестр extractor'ов по типу сущности.
DISPLAY_EXTRACTORS = {
    "Man": man_display,
    "ProjectBFL": projectbfl_display,
    "Money": money_display,
    "MessageWSP": messagewsp_display,
    "Files": files_display,
}


def extract_display(entity: str, raw: dict) -> dict:
    fn = DISPLAY_EXTRACTORS.get(entity)
    if fn:
        return fn(raw)
    return {"display_title": "", "display_subtitle": "", "bubble_created": None}
