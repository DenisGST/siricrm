"""Генератор ТЕКСТА сообщения для публикации в газете «Коммерсантъ».

Поток: выбрали тип сообщения (введение реализации / реструктуризации) → проверили
недостающие данные → «Сгенерировать текст» подставляет данные CRM в шаблон типа →
текст правится вручную → «Сохранить». Дальше текст уходит в бланк заявки (blank.py).

🛑 Базовый контекст переиспользуем из apps.efrsb.generator — набор плейсхолдеров
   (должник / ФУ / СРО / суд / дело) там ровно тот же, дублировать 30 ключей и
   потом расходиться в формулировках хуже, чем импорт между двумя модулями
   публикаций. Здесь добавляем только то, чего в ЕФРСБ нет: поля бланка ИД
   (контакты одной строкой, прежнее ФИО, фактический адрес, чекбоксы).

🛑 Требования ИД к тексту (п. 4 Порядка, утв. Приказом Минэкономразвития РФ
   от 12.07.2010 № 292): сокращения не допускаются, кроме предусмотренных НПА.
   ИД вправе отказать, если сообщение не позволяет идентифицировать должника и
   управляющего (п. 8 ст. 28 ФЗ № 127-ФЗ). Поэтому check_problems здесь строгий:
   пустой ИНН/СНИЛС/адрес — это отказ в публикации, а не косметика.
"""
from __future__ import annotations

import logging
import re

from apps.efrsb.generator import (
    _procedure_for,
    build_context as _base_context,
    placeholders_in,
    substitute,
)
from apps.procedure.request_documents import _fmt

log = logging.getLogger(__name__)


class KommersantGenError(RuntimeError):
    pass


# ── контекст ────────────────────────────────────────────────────────────────

def _contacts(*parts) -> str:
    """«Тел., факс, е-mail» одной строкой — в бланке ИД это одно поле."""
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _former_fio(client) -> str:
    """Прежнее ФИО должника (бланк ИД: «ФИО гражданина, если менялось, до изменения»)."""
    nh = client.name_history.first() if hasattr(client, "name_history") else None
    if nh is None:
        return ""
    fio = " ".join(filter(None, [nh.last_name, nh.first_name, nh.patronymic])).strip()
    # Запись без изменений (та же фамилия) в бланк не идёт — ИД читает это как «менялось».
    if not fio or (nh.last_name and nh.last_name == client.last_name and not nh.first_name):
        return ""
    return fio


def _fu_approved(am) -> str:
    """«утверждён» / «утверждена» — согласование по полу управляющего.

    Пол берём по отчеству: женские отчества оканчиваются на -вна/-чна, мужские
    на -ич. Отчества нет или нестандартное — даём мужскую форму (в бланке ИД пола
    нет, а вписывать его в справочник ради одного слова избыточно).
    🛑 Это эвристика: если она промахнулась, АУ правит формулировку в тексте
    сообщения перед сохранением либо в шаблоне типа (Справочники).
    """
    patronymic = (getattr(am, "patronymic", "") or "").strip().lower()
    if patronymic.endswith(("вна", "чна")):
        return "утверждена"
    return "утверждён"


def _actual_address(client) -> str:
    """Фактический адрес — только если он ЕСТЬ отдельно от регистрации.

    Бланк ИД просит его лишь «при отсутствии места регистрации», поэтому
    дублировать сюда адрес регистрации нельзя.
    """
    addr = client.addresses.filter(address_type="actual").first()
    return (addr.result or addr.source or "") if addr else ""


def build_context(case, *, message_type=None, procedure=None, overrides=None) -> dict:
    """Плоский dict плейсхолдеров: базовые (ЕФРСБ) + поля бланка «Коммерсанта»."""
    overrides = dict(overrides or {})
    ctx = _base_context(case, procedure=procedure)
    client = case.service.client
    proc = _procedure_for(case, procedure)
    am = proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None

    ctx.update({
        "Тип сообщения": (message_type.name if message_type else ""),
        # Поля бланка заявки ИД, которых нет в контексте ЕФРСБ
        "контакты АУ": _contacts(am.phone if am else "", am.email if am else ""),
        "контакты должника": _contacts(client.phone or "", client.email or ""),
        "прежнее ФИО": _former_fio(client),
        "утверждён ФУ": _fu_approved(am),
        "адрес фактический": _actual_address(client),
        "дата публикации Коммерсантъ": _fmt(proc.publication_kommersant_date) if proc else "",
    })
    ctx.update({k: v for k, v in overrides.items() if v is not None})
    return ctx


# ── шаблон текста ───────────────────────────────────────────────────────────

def resolve_text_template(message_type) -> str:
    if message_type is not None and (message_type.text_template or "").strip():
        return message_type.text_template
    return ""


def render_message_text(case, message_type, *, procedure=None, overrides=None) -> str:
    """Сгенерировать текст сообщения по шаблону типа."""
    tpl = resolve_text_template(message_type)
    if not tpl.strip():
        raise KommersantGenError(
            "У типа сообщения не задан шаблон текста. Заполните «Шаблон текста сообщения» "
            "в справочнике (или введите текст вручную)."
        )
    ctx = build_context(case, message_type=message_type, procedure=procedure, overrides=overrides)
    return substitute(tpl, ctx).strip()


# ── проверка данных (показываем ТОЛЬКО проблемы) ────────────────────────────

_LABELS = {
    "ФИО должника": "ФИО должника",
    "дата рождения": "Дата рождения должника",
    "место рождения": "Место рождения должника",
    "ИНН": "ИНН должника", "СНИЛС": "СНИЛС должника",
    "адрес регистрации": "Адрес регистрации должника",
    "ФИО Финансовый управляющий": "ФИО финуправляющего",
    "ИНН АУ": "ИНН финуправляющего", "СНИЛС АУ": "СНИЛС финуправляющего",
    "Адрес арбитражного управляющего": "Адрес корреспонденции ФУ",
    "email арбитражного": "E-mail финуправляющего",
    "контакты АУ": "Телефон / e-mail финуправляющего",
    "Реквизиты СРО": "СРО", "СРО полностью": "СРО (наименование, ИНН, ОГРН, адрес)",
    "арбитражный суд": "Арбитражный суд", "арбитражного суда": "Арбитражный суд",
    "адрес суда": "Адрес арбитражного суда",
    "номер дела": "Номер дела",
    "дата решения": "Дата судебного акта (введение процедуры)",
    "срок процедуры": "Срок процедуры, мес.",
    "дата следующего заседания": "Дата следующего судебного заседания",
}
_DIGIT_RULES = {"ИНН": (10, 12), "ИНН АУ": (10, 12), "СНИЛС": (11,), "СНИЛС АУ": (11,)}

# Поля, которые в бланке ИД могут быть пустыми на законных основаниях.
_OPTIONAL_KEYS = {"прежнее ФИО", "адрес фактический", "контакты должника"}


def check_problems(case, message_type, *, procedure=None, keys=None) -> list[dict]:
    """Только НЕзаполненные/невалидные данные. [] — всё в порядке.

    `keys` — проверить конкретный набор ключей (используется предпроверкой бланка,
    у которого свой набор полей). По умолчанию — плейсхолдеры шаблона текста.
    """
    if keys is None:
        tpl = resolve_text_template(message_type)
        if not tpl.strip():
            return [{"label": "Шаблон текста сообщения",
                     "note": "не задан у типа — текст можно ввести вручную"}]
        keys = placeholders_in(tpl)

    ctx = build_context(case, message_type=message_type, procedure=procedure)
    problems = []
    for key in keys:
        if key in _OPTIONAL_KEYS:
            continue
        val = str(ctx.get(key) or "").strip()
        label = _LABELS.get(key, key)
        if not val:
            problems.append({"label": label, "note": "не заполнено"})
            continue
        rule = _DIGIT_RULES.get(key)
        if rule and len(re.sub(r"\D", "", val)) not in rule:
            problems.append({"label": label, "note": "неверный формат"})
            continue
        if key == "email арбитражного" and "@" not in val:
            problems.append({"label": label, "note": "неверный e-mail"})
    return problems
