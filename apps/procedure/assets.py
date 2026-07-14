"""Активы должника: сохранение результата парсинга справки ФНС в БД.

Парсер (`fns_parser`) даёт чистый dict — здесь он раскладывается по моделям.
Повторная загрузка справки (ФНС отвечает не один раз за дело) не плодит дубли:
записи обновляются по естественным ключам (номер счёта, кадастровый номер, VIN,
год+агент 2-НДФЛ) в рамках пары «дело + субъект (должник/супруг)».
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.crm.models import LegalEntity

from .models import (
    AssetDocument,
    BankAccount,
    IncomeCertificate,
    LandPlot,
    OtherAsset,
    RealEstateObject,
    Vehicle,
)


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def match_bank(inn: str, bik: str = "") -> LegalEntity | None:
    """Банк из справки → запись реестра `crm.LegalEntity` (адресат запроса выписки).

    ИНН у банка уникален, поэтому матч точный. Один банк может идти в справке
    несколькими блоками (Сбер: головной + отделения, разные КПП/БИК) — все они
    ложатся на одну запись реестра.
    """
    if inn:
        entity = LegalEntity.objects.filter(inn=inn).order_by("created_at").first()
        if entity:
            return entity
    if bik:
        return LegalEntity.objects.filter(bik=bik).order_by("created_at").first()
    return None


@transaction.atomic
def save_parsed(case, data: dict, *, stored_file=None, filename: str = "", employee=None) -> dict:
    """Разложить результат парсинга по моделям. Возвращает счётчики сохранённого."""
    subject = data.get("subject") or "debtor"

    doc = AssetDocument.objects.create(
        case=case,
        stored_file=stored_file,
        filename=filename or (stored_file.filename if stored_file else ""),
        subject=subject,
        subject_fio=data.get("subject_fio", "")[:255],
        subject_inn=data.get("subject_inn", "")[:12],
        subject_birth_date=_date(data.get("subject_birth_date")),
        debtor_fio=data.get("debtor_fio", "")[:255],
        debtor_inn=data.get("debtor_inn", "")[:12],
        court_name=data.get("court_name", "")[:255],
        case_number=data.get("case_number", "")[:64],
        formed_at=_date(data.get("formed_at")),
        tax_authority=data.get("tax_authority", "")[:400],
        has_tax_debt=data.get("has_tax_debt"),
        raw=data,
        uploaded_by=employee,
    )

    stats = {"accounts": 0, "incomes": 0, "realty": 0, "land": 0, "vehicles": 0, "other": 0,
             "banks_matched": 0, "banks_unknown": 0}

    # ── Счета ──
    for acc in data.get("accounts", []):
        entity = match_bank(acc.get("bank_inn", ""), acc.get("bank_bik", ""))
        if entity:
            stats["banks_matched"] += 1
        else:
            stats["banks_unknown"] += 1
        BankAccount.objects.update_or_create(
            case=case, subject=subject, number=acc["number"],
            defaults={
                "document": doc,
                "opened_date": _date(acc.get("opened_date")),
                "closed_date": _date(acc.get("closed_date")),
                "state": acc.get("state", ""),
                "state_text": acc.get("state_text", "")[:120],
                "account_kind": acc.get("account_kind", "")[:120],
                "bank_name": acc.get("bank_name", "")[:400],
                "bank_inn": acc.get("bank_inn", "")[:12],
                "bank_kpp": acc.get("bank_kpp", "")[:9],
                "bank_bik": acc.get("bank_bik", "")[:12],
                "bank_regnum": acc.get("bank_regnum", "")[:32],
                "bank_address": acc.get("bank_address", "")[:500],
                "legal_entity": entity,
            },
        )
        stats["accounts"] += 1

    # ── 2-НДФЛ ──
    for cert in data.get("incomes", []):
        IncomeCertificate.objects.update_or_create(
            case=case, subject=subject, year=cert["year"],
            agent_inn=cert.get("agent_inn", ""), cert_date=_date(cert.get("cert_date")),
            defaults={
                "document": doc,
                "agent_name": cert.get("agent_name", "")[:500],
                "agent_kpp": cert.get("agent_kpp", "")[:9],
                "oktmo": cert.get("oktmo", "")[:11],
                "total_income": _dec(cert.get("total_income")),
                "tax_base": _dec(cert.get("tax_base")),
                "tax_calculated": _dec(cert.get("tax_calculated")),
                "tax_withheld": _dec(cert.get("tax_withheld")),
                "rows": cert.get("rows", []),
            },
        )
        stats["incomes"] += 1

    # ── Недвижимость / земля / транспорт ──
    for obj in data.get("realty", []):
        RealEstateObject.objects.update_or_create(
            case=case, subject=subject,
            cadastral_number=obj.get("cadastral_number", ""),
            address=obj.get("address", "")[:600],
            defaults={
                "document": doc,
                "object_type": obj.get("object_type", "")[:200],
                "area": obj.get("area", "")[:32],
                "share": obj.get("share", "")[:32],
                "cadastral_value": _dec(obj.get("cadastral_value")),
                "commissioned_date": _date(obj.get("commissioned_date")),
                "reg_date": _date(obj.get("reg_date")),
                "dereg_date": _date(obj.get("dereg_date")),
            },
        )
        stats["realty"] += 1

    for obj in data.get("land", []):
        LandPlot.objects.update_or_create(
            case=case, subject=subject,
            cadastral_number=obj.get("cadastral_number", ""),
            address=obj.get("address", "")[:600],
            defaults={
                "document": doc,
                "category": obj.get("category", "")[:200],
                "area": obj.get("area", "")[:32],
                "share": obj.get("share", "")[:32],
                "cadastral_value": _dec(obj.get("cadastral_value")),
                "reg_date": _date(obj.get("reg_date")),
                "dereg_date": _date(obj.get("dereg_date")),
            },
        )
        stats["land"] += 1

    for obj in data.get("vehicles", []):
        Vehicle.objects.update_or_create(
            case=case, subject=subject,
            vin=obj.get("vin", ""), plate=obj.get("plate", ""),
            defaults={
                "document": doc,
                "ownership_kind": obj.get("ownership_kind", "")[:200],
                "year": obj.get("year", "")[:8],
                "model": obj.get("model", "")[:200],
                "power": obj.get("power", "")[:32],
                "reg_authority": obj.get("reg_authority", "")[:120],
                "pts": obj.get("pts", "")[:64],
                "reg_date": _date(obj.get("reg_date")),
                "dereg_date": _date(obj.get("dereg_date")),
            },
        )
        stats["vehicles"] += 1

    # ── Иное: налоговая задолженность, участие в ЮЛ, адм. правонарушения ──
    if data.get("has_tax_debt") is not None:
        OtherAsset.objects.update_or_create(
            case=case, subject=subject, kind=OtherAsset.KIND_TAX_DEBT,
            title="Задолженность по налогам, сборам, взносам",
            defaults={
                "document": doc,
                "details": ("По справке ФНС: неисполненная обязанность ЕСТЬ"
                            if data["has_tax_debt"] else
                            "По справке ФНС: неисполненной обязанности нет"),
                "date": _date(data.get("formed_at")),
            },
        )
        stats["other"] += 1

    for item in data.get("admin", []):
        OtherAsset.objects.update_or_create(
            case=case, subject=subject, kind=OtherAsset.KIND_ADMIN_OFFENSE,
            title=item["title"][:400],
            defaults={
                "document": doc,
                "details": item.get("details", ""),
                "amount": _dec(item.get("amount")),
                "date": _date(item.get("date")),
            },
        )
        stats["other"] += 1

    for item in data.get("legal_entities", []):
        OtherAsset.objects.update_or_create(
            case=case, subject=subject, kind=OtherAsset.KIND_LEGAL_ENTITY,
            title=item["title"][:400],
            defaults={"document": doc, "details": item.get("details", "")},
        )
        stats["other"] += 1

    return {"document": doc, **stats}
