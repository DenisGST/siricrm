"""Резолв ссылок Bubble (_id) на справочники SiriCRM.

Bubble FK — это _id записи справочника. Чтобы не дёргать API на каждый
объект, при первом обращении выкачиваем весь справочник целиком
(они маленькие: Region 92, MoneySource/TypeDebit/TypeCredit/TypeAccrual —
десятки) и кэшируем на время процесса.

Кэш живёт в рамках одного процесса. Для повторного импорта после
изменений в Bubble — перезапустить worker/web (или вызвать clear_cache).
"""
import functools
import logging

from . import bubble_api
from .extractors import clean_str

logger = logging.getLogger("bubble_import")


@functools.lru_cache(maxsize=None)
def _load(entity: str) -> dict:
    """Весь справочник Bubble → {_id: raw_obj}."""
    objs = {}
    for o in bubble_api.iter_all(entity):
        bid = o.get("_id")
        if bid:
            objs[bid] = o
    logger.info("resolvers: загружен справочник %s (%d)", entity, len(objs))
    return objs


def clear_cache():
    _load.cache_clear()


def lookup(entity: str, bubble_id: str, field: str) -> str:
    """Значение поля объекта справочника Bubble по его _id."""
    if not bubble_id:
        return ""
    obj = _load(entity).get(bubble_id)
    if not obj:
        return ""
    return clean_str(obj.get(field, ""))


# ─── Доменные резолверы ────────────────────────────────────

# Маппинг статуса услуги Bubble (StatusPrj) → статус клиента SiriCRM.
# Ключи — нормализованные (lower) названия nameStatusPrj.
_STATUS_PRJ_TO_CLIENT = {
    "неразобран": "unknown",
    "анкетирование": "lead",
    "думают": "lead",
    "согласование": "lead",
    "заключение договора": "lead",
    "сбор документов": "active",
    "подготовка иска": "active",
    "ввод": "active",
    "реструктуризация": "active",
    "реализация": "active",
    "завершение": "active",
    "приостановка договора": "active",
    "завершен": "archive",
    "отказ": "refused",
    "договор расторгнут": "refused",
    "на удаление": "to_delete",
}


def resolve_client_status(status_prj_bubble_id: str):
    """Bubble StatusPrj._id → код статуса crm.Client. None если не сопоставлен."""
    name = lookup("StatusPrj", status_prj_bubble_id, "nameStatusPrj")
    return _STATUS_PRJ_TO_CLIENT.get(name.strip().lower()) if name else None


@functools.lru_cache(maxsize=None)
def _projects_by_client() -> dict:
    """Индекс ProjectBFL по клиенту: {dolgnik_id: [project, ...]}.

    Выкачивает все услуги один раз (≈5400) — чтобы при импорте клиентов
    определять их статус без отдельного запроса на каждого.
    """
    idx: dict = {}
    for o in _load("ProjectBFL").values():
        d = o.get("dolgnik")
        if d:
            idx.setdefault(d, []).append(o)
    return idx


def resolve_client_status_by_man(man_bubble_id: str):
    """Статус клиента по его услуге(ам) ProjectBFL. None если услуг нет.

    Если услуг несколько — берём по самой поздней (Created Date).
    """
    projects = _projects_by_client().get(man_bubble_id)
    if not projects:
        return None
    latest = max(projects, key=lambda p: p.get("Created Date", ""))
    return resolve_client_status(latest.get("statusPrj"))


def resolve_region(region_bubble_id: str):
    """Bubble Region._id → crm.Region (по numberRegion). None если нет."""
    num = lookup("Region", region_bubble_id, "numberRegion")
    if not num:
        return None
    from apps.crm.models import Region
    try:
        return Region.objects.filter(number=int(float(num))).first()
    except (ValueError, TypeError):
        return None


def resolve_bfl_service_name():
    """ServiceName «БФЛ» — все импортируемые услуги этого типа."""
    from apps.crm.models import ServiceName
    sn = ServiceName.objects.filter(short_name__iexact="БФЛ").first()
    if sn is None:
        sn = ServiceName.objects.create(
            short_name="БФЛ", full_name="Банкротство физических лиц",
        )
    return sn


def resolve_income_type(type_debit_bubble_id: str):
    """Bubble TypeDebit._id → finance.IncomeType (get_or_create по name)."""
    name = lookup("TypeDebit", type_debit_bubble_id, "name")
    if not name:
        return None
    from apps.finance.models import IncomeType
    obj, _ = IncomeType.objects.get_or_create(
        service_name=resolve_bfl_service_name(), name=name[:255],
    )
    return obj


def resolve_expense_type(type_credit_bubble_id: str):
    """Bubble TypeCredit._id → finance.ExpenseType (get_or_create по name)."""
    name = lookup("TypeCredit", type_credit_bubble_id, "name")
    if not name:
        return None
    from apps.finance.models import ExpenseType
    obj, _ = ExpenseType.objects.get_or_create(
        service_name=resolve_bfl_service_name(), name=name[:255],
    )
    return obj


def resolve_incoming_account(money_source_bubble_id: str):
    """Bubble MoneySource._id → finance.IncomingAccount (get_or_create)."""
    name = lookup("MoneySource", money_source_bubble_id, "name")
    if not name:
        return None
    from apps.finance.models import IncomingAccount
    acc_type = "cash" if "касс" in name.lower() else "bank"
    obj, _ = IncomingAccount.objects.get_or_create(
        account_type=acc_type, name=name[:255],
    )
    return obj


def resolve_outgoing_account(money_source_bubble_id: str):
    """Bubble MoneySource._id → finance.OutgoingAccount (get_or_create)."""
    name = lookup("MoneySource", money_source_bubble_id, "name")
    if not name:
        return None
    from apps.finance.models import OutgoingAccount
    acc_type = "cash" if "касс" in name.lower() else "bank"
    obj, _ = OutgoingAccount.objects.get_or_create(
        account_type=acc_type, name=name[:255],
    )
    return obj
