"""Парсинг и нормализация полей Bubble.

Особенности данных Bubble (выяснены при сверке структуры):
* числовые поля могут приходить и числом, и строкой (inn/INN, PaspSer/PassSer);
* даты — ISO-8601 с Z, но dateTimeInMessage в MessageWSP битое — не использовать;
* телефоны — 10 цифр без префикса страны;
* Bubble отдаёт только непустые поля объекта.
"""
import datetime
import re

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


# Реестр extractor'ов по типу сущности (расширяется на этапах B4+).
DISPLAY_EXTRACTORS = {
    "Man": man_display,
}


def extract_display(entity: str, raw: dict) -> dict:
    fn = DISPLAY_EXTRACTORS.get(entity)
    if fn:
        return fn(raw)
    return {"display_title": "", "display_subtitle": "", "bubble_created": None}
