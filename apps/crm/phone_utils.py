"""Утилиты работы с телефонами клиента: нормализация, поиск, синхронизация.

`ClientPhone` — источник правды. `Client.phone` / `Client.whatsapp_phone` —
кэш, обновляются через `sync_client_phone_cache`.
"""
from __future__ import annotations

import re
from typing import Iterable

from django.db import IntegrityError, transaction

from .models import Client, ClientPhone


def normalize_phone(raw) -> str:
    """Привести любой формат к 11-значному E.164 без «+».

    Возвращает «» если телефон невалидный (не 11 цифр или не начинается с 7).
    Например: «+7 (910) 555-01-01», «8 910 555 01 01», «79105550101», «79105550101@c.us»
    → «79105550101».
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("7"):
        return digits
    return ""


def find_client_by_phone(
    phone: str, purposes: Iterable[str] | None = None
) -> Client | None:
    """Найти клиента по любому номеру в любом назначении (или ограниченном
    наборе назначений). Сначала пробуем строгое совпадение в нужных
    purpose'ах, затем — любой purpose."""
    phone = normalize_phone(phone)
    if not phone:
        return None
    qs = ClientPhone.objects.filter(phone=phone, is_active=True)
    if purposes:
        narrowed = qs.filter(purpose__in=list(purposes)).select_related("client").first()
        if narrowed is not None:
            return narrowed.client
    cp = qs.select_related("client").first()
    return cp.client if cp else None


def add_client_phone(
    client: Client, phone: str, purpose: str = "additional"
) -> ClientPhone | None:
    """Добавить телефон клиенту. Idempotent: повторный вызов с тем же
    (client, phone, purpose) вернёт существующую запись. Конфликт по
    UniqueConstraint(phone, purpose) с **другим** клиентом — возвращает
    None (телефон уже занят кем-то ещё в этом назначении)."""
    phone = normalize_phone(phone)
    if not phone:
        return None
    existing = ClientPhone.objects.filter(phone=phone, purpose=purpose).first()
    if existing is not None:
        return existing if existing.client_id == client.id else None
    try:
        with transaction.atomic():
            return ClientPhone.objects.create(
                client=client, phone=phone, purpose=purpose,
            )
    except IntegrityError:
        return ClientPhone.objects.filter(phone=phone, purpose=purpose).first()


def sync_client_phone_cache(client: Client, save: bool = True) -> None:
    """Обновить кэш-поля Client.phone / Client.whatsapp_phone из ClientPhone.
    Берём первый активный telephon по purpose."""
    primary = (
        client.phones.filter(purpose="primary", is_active=True).first()
        or client.phones.filter(purpose="additional", is_active=True).first()
    )
    wa = client.phones.filter(purpose="whatsapp", is_active=True).first()

    changed: list[str] = []
    new_phone = ("+" + primary.phone) if primary else ""
    if (client.phone or "") != new_phone:
        client.phone = new_phone or None
        changed.append("phone")
    new_wa = wa.phone if wa else None
    if (client.whatsapp_phone or None) != new_wa:
        # whatsapp_phone unique=True — пишем только если этот номер не занят
        # другим клиентом (на уровне Client). ClientPhone уже даёт гарантию,
        # но Client.whatsapp_phone — отдельный legacy-unique, проверим.
        if new_wa is None or not Client.objects.exclude(pk=client.pk).filter(
            whatsapp_phone=new_wa
        ).exists():
            client.whatsapp_phone = new_wa
            changed.append("whatsapp_phone")

    if changed and save:
        client.save(update_fields=changed)
