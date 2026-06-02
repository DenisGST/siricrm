"""Договор юруслуг БФЛ: проверка реквизитов и сборка контекста плейсхолдеров.

Маппинг плейсхолдеров шаблона `OLD/Шаблон договора юруслуги по банкротству.docx`
на поля Client / Service / ExecutorOrg.
"""
from decimal import Decimal

from .models import ExecutorOrg


# ── Форматирование ──────────────────────────────────────────────────────────
def _fmt_date(d):
    return d.strftime("%d.%m.%Y") if d else ""


def _fmt_rub(value):
    """Целое число рублей с разделителями тысяч (для «… рублей 00 копеек»)."""
    if value is None:
        return "0"
    return f"{int(Decimal(value)):,}".replace(",", " ")


def _registration_address(client):
    addr = client.addresses.filter(address_type="registration").first()
    if addr is None:
        addr = client.addresses.filter(address_type="actual").first()
    return addr


def _primary_phone(client):
    p = client.phones.filter(purpose="primary", is_active=True).first()
    if p:
        return p.phone
    return client.phone or ""


# ── Проверка реквизитов ───────────────────────────────────────────────────────
def check_requisites(service):
    """Возвращает (ok: bool, groups: list).

    groups — список секций для UI:
      {"title": str, "items": [{"label", "value", "ok", "required"}]}
    ok=True, если все required-поля заполнены.
    """
    client = service.client
    addr = _registration_address(client)
    phone = _primary_phone(client)
    executor = ExecutorOrg.get_default()

    def item(label, value, *, required=True):
        val = (str(value).strip() if value not in (None, "") else "")
        return {"label": label, "value": val, "ok": bool(val), "required": required}

    client_items = [
        item("Фамилия", client.last_name),
        item("Имя", client.first_name),
        item("Отчество", client.patronymic),
        item("Дата рождения", _fmt_date(client.birth_date)),
        item("Серия паспорта", client.passport_series),
        item("Номер паспорта", client.passport_number),
        item("Кем выдан паспорт", client.passport_issued_by),
        item("Дата выдачи паспорта", _fmt_date(client.passport_issued_date)),
        item("Адрес регистрации", addr.result if addr else ""),
        item("Почтовый индекс", addr.postal_code if addr else "", required=False),
        item("Телефон", phone),
    ]

    service_items = [
        item("Номер договора", service.numb_dogovor),
        item("Дата договора", _fmt_date(service.date_dogovor)),
        item("Дата начала услуг", _fmt_date(service.date_start)),
        item("Сумма юруслуг", _fmt_rub(service.legal_services_amount) if service.legal_services_amount else ""),
        item("График платежей составлен", _fmt_date(service.schedule_date)),
    ]

    executor_items = [
        item("Организация-исполнитель", executor.name if executor else ""),
        item("Вводная строка (ispolnitel)", executor.intro_text if executor else ""),
        item("Реквизиты исполнителя", executor.requisites if executor else ""),
        item("ФИО подписанта", executor.signer_name if executor else ""),
    ]

    groups = [
        {"title": "Клиент (Заказчик)", "items": client_items},
        {"title": "Договор / услуга", "items": service_items},
        {"title": "Исполнитель", "items": executor_items},
    ]
    ok = all(it["ok"] for g in groups for it in g["items"] if it["required"])
    return ok, groups


# ── Сборка контекста ──────────────────────────────────────────────────────────
def _payment_lines(service):
    """Строки графика по юруслугам → {1платеж}..{12платеж}.

    Заполняем ровно installment_months строк, остальные слоты — пустые.
    """
    legal_charges = [
        ch for ch in service.charges.order_by("due_date")
        if (ch.title or "").startswith("Юруслуги")
    ]
    lines = {}
    for i in range(1, 13):
        key = f"{i}платеж"
        if i <= len(legal_charges):
            ch = legal_charges[i - 1]
            lines[key] = (
                f"Платёж {i}: до {_fmt_date(ch.due_date)} — {_fmt_rub(ch.amount)} руб."
            )
        else:
            lines[key] = ""
    return lines


def _dop_line(service):
    """{summDop} — отдельная строка доп. расходов (additional_costs)."""
    if service.additional_costs and service.additional_costs > 0:
        return f"-\tДополнительные расходы – {_fmt_rub(service.additional_costs)} рублей 00 копеек;"
    return ""


def build_context(service, executor=None):
    """Собирает dict плейсхолдеров (ключи БЕЗ фигурных скобок)."""
    client = service.client
    addr = _registration_address(client)
    executor = executor or ExecutorOrg.get_default()
    full_name = f"{client.last_name} {client.first_name} {client.patronymic}".strip()

    ctx = {
        # Шапка / стороны
        "numb_dogovor": service.numb_dogovor or "",
        "date_dogovor": _fmt_date(service.date_dogovor),
        "регион": service.region.name if service.region_id else "",
        "ispolnitel": (executor.intro_text if executor else "") or "",
        # Заказчик
        "Фамилия": client.last_name or "",
        "Имя": client.first_name or "",
        "Отчество": client.patronymic or "",
        "дата рождения": _fmt_date(client.birth_date),
        "паспорт_серия": client.passport_series or "",
        "паспорт_номер": client.passport_number or "",
        "паспорт_выдан_где": client.passport_issued_by or "",
        "паспорт_выдан_когда": _fmt_date(client.passport_issued_date),
        "адрес_регистрации": (addr.result if addr else "") or "",
        "индекс": (addr.postal_code if addr else "") or "",
        "номер_телефона": _primary_phone(client),
        # Суммы
        "сумма_юруслуги": _fmt_rub(service.legal_services_amount),
        "сумма_сбор": _fmt_rub(service.doc_collection),
        "сумма_почта": _fmt_rub(service.postal_costs),
        "сумма_финуправляющий": _fmt_rub(service.fu_fee),
        "сумма_публикации": _fmt_rub(service.procedure_costs),
        "summDop": _dop_line(service),
        # Подписи / реквизиты
        "Реквизиты_исполнителя": (executor.requisites if executor else "") or "",
        "Исполнитель": (executor.signer_name if executor else "") or "",
        "Заказчик": full_name,
    }
    ctx.update(_payment_lines(service))
    return ctx
