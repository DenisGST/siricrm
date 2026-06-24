"""Коннекторы к ТБанк.

1) Выписка р/с — T-API «Бизнес», pull: `GET {BASE}/api/v1/statement` (Bearer токен).
   Поллинг по Celery (tasks.py). Парсим только входящие (кредит счёта).

2) Эквайринг — интернет-эквайринг НЕ поддерживает «список операций за период»,
   приём только через нотификации (webhook). Поэтому здесь — helpers разбора и
   проверки подписи нотификации (вызываются из views.acquiring_webhook), а не
   поллинг. `fetch_incoming('acquiring', …)` не используется.

Нормализованная операция (для tasks._poll):
  external_id, occurred_at (aware dt), amount (Decimal),
  payer_name, payer_inn, payer_phone, purpose, order_id, raw
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

from .models import IncomingPayment

log = logging.getLogger(__name__)

_TIMEOUT = 30


def is_configured(source: str) -> bool:
    if source == IncomingPayment.SOURCE_ACQUIRING:
        return bool(settings.TBANK_ACQUIRING_TERMINAL_KEY and settings.TBANK_ACQUIRING_PASSWORD)
    return bool(settings.TBANK_BUSINESS_API_TOKEN and settings.TBANK_ACCOUNT_NUMBER)


# ── Выписка р/с (T-API «Бизнес») ────────────────────────────────────────────

def _statement_get(params: dict) -> dict:
    url = f"{settings.TBANK_BUSINESS_API_BASE.rstrip('/')}/api/v1/statement"
    headers = {
        "Authorization": f"Bearer {settings.TBANK_BUSINESS_API_TOKEN}",
        "X-Request-Id": str(uuid.uuid4()),
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _extract_operations(payload) -> list:
    """Достать массив операций из ответа (имя поля уточняется по факту)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("operations", "transactions", "items", "data", "result"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def _g(op: dict, *keys, default=""):
    """Первое непустое значение по списку возможных ключей."""
    for k in keys:
        if k in op and op[k] not in (None, ""):
            return op[k]
    return default


def _is_incoming(op: dict) -> bool:
    # ТБанк: typeOfOperation == "Credit" — поступление на счёт (проверено на live).
    return str(op.get("typeOfOperation", "")).lower() == "credit"


# ИНН банков-эквайеров. Зачисление с таким плательщиком в выписке — это сводный
# эквайринг (терминал/СБП), а не прямой перевод клиента.
ACQUIRER_INNS = {"7710140679"}  # АО «ТБанк» / Тинькофф Банк


def is_acquiring_settlement(payer_name: str, payer_inn: str) -> bool:
    name = (payer_name or "").lower()
    if (payer_inn or "") in ACQUIRER_INNS:
        return True
    return "тбанк" in name or "тинькофф" in name


def _to_decimal(val) -> Decimal:
    if isinstance(val, dict):  # бывает {"value": .., "currency": ..}
        val = val.get("value") or val.get("amount") or 0
    try:
        return Decimal(str(val))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _to_dt(val):
    if not val:
        return timezone.now()
    if isinstance(val, (int, float)):  # epoch
        return datetime.fromtimestamp(val, tz=timezone.get_current_timezone())
    s = str(val).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return timezone.now()
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def normalize_statement_op(op: dict) -> dict:
    # Для входящего (Credit) отправитель — объект `payer` (fallback counterParty).
    payer = op.get("payer") or op.get("counterParty") or {}
    if not isinstance(payer, dict):
        payer = {}
    return {
        "external_id": str(op.get("operationId") or op.get("documentNumber") or uuid.uuid4().hex),
        "occurred_at": _to_dt(op.get("operationDate") or op.get("drawDate") or op.get("chargeDate")),
        "amount": _to_decimal(op.get("accountAmount") or op.get("operationAmount") or 0),
        "payer_name": payer.get("name", ""),
        "payer_inn": payer.get("inn", ""),
        "payer_phone": "",
        "purpose": op.get("payPurpose") or op.get("description") or "",
        "order_id": "",
        "is_settlement": is_acquiring_settlement(payer.get("name", ""), payer.get("inn", "")),
        "raw": op,
    }


def fetch_incoming(source: str, since) -> list[dict]:
    if source == IncomingPayment.SOURCE_ACQUIRING:
        # Эквайринг приходит через webhook (views.acquiring_webhook), не поллингом.
        return []

    params = {
        "accountNumber": settings.TBANK_ACCOUNT_NUMBER,
        "from": since.isoformat() if hasattr(since, "isoformat") else since,
        "operationStatus": "Transaction",  # только проведённые (не холды-авторизации)
        "limit": 1000,
    }
    result, cursor, guard = [], None, 0
    while True:
        if cursor:
            params["cursor"] = cursor
        payload = _statement_get(params)
        ops = _extract_operations(payload)
        for op in ops:
            if _is_incoming(op) and str(op.get("operationStatus", "Transaction")) == "Transaction":
                result.append(normalize_statement_op(op))
        cursor = payload.get("nextCursor") if isinstance(payload, dict) else None
        guard += 1
        if not cursor or guard > 50:
            break
    return result


# ── Эквайринг: проверка подписи и разбор нотификации ────────────────────────

def _token_value(v) -> str:
    # 🛑 ТБанк кладёт булево как "true"/"false" (нижний регистр), а не Python "True".
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def acquiring_token(params: dict, password: str) -> str:
    """Token интернет-эквайринга: SHA-256 от конкатенации значений корневых
    параметров (без вложенных объектов и самого Token) + Password, в порядке
    сортировки ключей."""
    pairs = {k: v for k, v in params.items()
             if k != "Token" and not isinstance(v, (dict, list))}
    pairs["Password"] = password
    concat = "".join(_token_value(pairs[k]) for k in sorted(pairs))
    return hashlib.sha256(concat.encode("utf-8")).hexdigest()


def validate_acquiring_notification(data: dict) -> bool:
    token = data.get("Token", "")
    expected = acquiring_token(data, settings.TBANK_ACQUIRING_PASSWORD)
    return bool(token) and token.lower() == expected.lower()


def parse_acquiring_notification(data: dict) -> dict:
    """Нотификация эквайринга → нормализованная входящая операция.

    Amount — в копейках. 🛑 ТБанк НЕ возвращает введённые ФИО/телефон (поле DATA
    обратно не отдаётся, в GetState их тоже нет). Поэтому телефон клиента берём
    из `OrderId`, куда страница оплаты fo-y.ru кладёт его как «<телефон>_<метка>».
    ФИО потом подтянется из карточки клиента (матч по телефону).
    """
    extra = data.get("DATA") or {}
    if not isinstance(extra, dict):
        extra = {}
    order_id = str(data.get("OrderId", ""))

    # ФИО/телефон приходят не от ТБанка, а из prepay (склейка по OrderId во вьюхе).
    # Здесь оставляем только DATA-fallback на случай, если поля когда-то придут.
    phone = extra.get("Phone") or extra.get("phone") or ""
    name = extra.get("Name") or extra.get("name") or extra.get("FIO") or ""
    return {
        "external_id": str(data.get("PaymentId", "")),
        "occurred_at": timezone.now(),
        "amount": _to_decimal(data.get("Amount", 0)) / Decimal("100"),
        "payer_name": name,
        "payer_inn": "",
        "payer_phone": str(phone or ""),
        "purpose": "Оплата через эквайринг (страница оплаты)",
        "order_id": order_id,
        "raw": data,
    }
