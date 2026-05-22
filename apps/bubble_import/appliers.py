"""APPLY-логика: одобренные BubbleRecord → продакшн-модели SiriCRM.

Этап B3 — только Man → Client. ProjectBFL/Money/MessageWSP/Files — на B4+.

Идемпотентность: повторный apply находит Client по bubble_id и обновляет.
Дедупликация: перед созданием нового клиента ищем совпадение по телефону —
если нашли чужого клиента, ставим запись в статус error (оператор решит).
"""
import logging

from django.db.models import Q
from django.utils import timezone

from apps.crm.models import Client, ClientNameHistory

from .extractors import (
    clean_str, first_nonempty, normalize_phone,
    gender_from_bubble, parse_bubble_date,
)
from .models import BubbleRecord

logger = logging.getLogger("bubble_import")


def _man_fields(rec: BubbleRecord) -> dict:
    """Собрать поля Client из записи Man (с учётом overrides оператора)."""
    v = rec.value
    return {
        "first_name": (clean_str(v("fName")) or "Без имени")[:255],
        "last_name": clean_str(v("lName"))[:255],
        "patronymic": clean_str(v("mName"))[:255],
        "birth_date": parse_bubble_date(v("dateR")),
        "birth_place": clean_str(v("cityR"))[:500],
        "passport_series": first_nonempty(v("PaspSer"), v("PassSer"))[:4],
        "passport_number": first_nonempty(v("PaspNumb"), v("passNumb"))[:6],
        "passport_issued_by": clean_str(v("passOut"))[:500],
        "passport_issued_date": parse_bubble_date(v("passDate")),
        "inn": first_nonempty(v("inn"), v("INN"))[:12],
        "snils": clean_str(v("snils"))[:14],
        "email": clean_str(v("email"))[:254],
        "notes": clean_str(v("notes")),
        "gender": gender_from_bubble(v("Пол")),
        "is_married": bool(v("isMarried")),
        "referral_source": clean_str(v("From"))[:255],
    }


def _apply_name_history(client: Client, rec: BubbleRecord):
    """Прежние ФИО из fNameOld / lNameOld / mNameOld."""
    v = rec.value
    old_last = clean_str(v("lNameOld"))
    old_first = clean_str(v("fNameOld"))
    old_patr = clean_str(v("mNameOld"))
    if not (old_last or old_first or old_patr):
        return
    ClientNameHistory.objects.get_or_create(
        client=client,
        last_name=old_last[:255],
        first_name=old_first[:255],
        patronymic=old_patr[:255],
        defaults={"note": "Импортировано из Bubble"},
    )


def apply_man(rec: BubbleRecord) -> str:
    """Перенести одну запись Man в Client. Возвращает итоговый статус."""
    bid = rec.bubble_id
    fields = _man_fields(rec)
    phone = normalize_phone(rec.value("tel"))

    client = Client.objects.filter(bubble_id=bid).first()

    # Новый клиент — проверка на дубль по телефону.
    if client is None and phone:
        dup = Client.objects.filter(
            Q(whatsapp_phone=phone) | Q(phone="+" + phone)
        ).first()
        if dup:
            rec.status = "error"
            rec.error = (
                f"Возможный дубль по телефону +{phone}: "
                f"уже есть клиент «{dup}» ({dup.id}). "
                f"Проверьте; при необходимости поправьте телефон и повторите."
            )
            rec.imported_at = None
            rec.save(update_fields=["status", "error", "imported_at"])
            return rec.status

    if client is None:
        client = Client(bubble_id=bid, **fields)
    else:
        for k, val in fields.items():
            setattr(client, k, val)

    if phone:
        client.phone = "+" + phone
        # whatsapp_phone уникален — ставим только если номер свободен.
        wa_taken = (
            Client.objects.filter(whatsapp_phone=phone)
            .exclude(pk=client.pk).exists()
        )
        if not wa_taken:
            client.whatsapp_phone = phone

    client.save()

    _apply_name_history(client, rec)

    rec.status = "imported"
    rec.target_type = "Client"
    rec.target_id = str(client.id)
    rec.error = ""
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


def link_spouses() -> int:
    """Связать супругов: Man.spouse (bubble_id) → Client.spouse FK.

    Запускается после apply пакета — оба супруга должны быть импортированы.
    Возвращает число проставленных связей.
    """
    linked = 0
    recs = BubbleRecord.objects.filter(
        entity="Man", status="imported",
    ).exclude(raw__spouse=None)
    by_bubble = {
        c.bubble_id: c
        for c in Client.objects.exclude(bubble_id=None)
    }
    for rec in recs:
        spouse_bid = (rec.raw or {}).get("spouse")
        client = by_bubble.get(rec.bubble_id)
        spouse = by_bubble.get(spouse_bid) if spouse_bid else None
        if client and spouse and client.spouse_id != spouse.id:
            client.spouse = spouse
            client.save(update_fields=["spouse"])
            linked += 1
    return linked


# Реестр applier'ов по типу сущности (расширяется на B4+).
APPLIERS = {
    "Man": apply_man,
}


def apply_record(rec: BubbleRecord) -> str:
    """Применить одну запись. Ошибки ловятся и пишутся в rec.error."""
    fn = APPLIERS.get(rec.entity)
    if fn is None:
        rec.status = "error"
        rec.error = f"Нет applier для сущности {rec.entity}"
        rec.save(update_fields=["status", "error"])
        return rec.status
    try:
        return fn(rec)
    except Exception as e:  # noqa: BLE001 — staging, ошибку показываем оператору
        logger.exception("apply %s/%s failed", rec.entity, rec.bubble_id)
        rec.status = "error"
        rec.error = f"{type(e).__name__}: {e}"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status


def apply_approved(entity: str) -> dict:
    """Применить все одобренные ещё не импортированные записи сущности."""
    qs = BubbleRecord.objects.filter(
        entity=entity, approved=True,
    ).exclude(status="imported")
    imported = errors = 0
    for rec in qs:
        st = apply_record(rec)
        if st == "imported":
            imported += 1
        else:
            errors += 1
    extra = {}
    if entity == "Man":
        extra["spouses_linked"] = link_spouses()
    return {"imported": imported, "errors": errors, **extra}
