"""Перенос Client.phone / Client.whatsapp_phone в ClientPhone.

Старые поля Client.phone и Client.whatsapp_phone остаются как кэш — пишутся
синхронно в applier'ах и формах. Источник правды — ClientPhone.
Конфликты по unique (phone, purpose) — пропускаем (звучит в stdout).
"""
import re

from django.db import migrations


def _norm(raw: str) -> str:
    """Привести к 11-значному E.164 без «+». «» если телефон невалидный."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("7"):
        return digits
    return ""


def forward(apps, schema_editor):
    Client = apps.get_model("crm", "Client")
    ClientPhone = apps.get_model("crm", "ClientPhone")

    # Сначала собираем уже занятые пары (phone, purpose), чтобы при bulk_create
    # не упасть на UniqueConstraint — Postgres откатит всю транзакцию миграции.
    taken: set[tuple[str, str]] = set(
        ClientPhone.objects.values_list("phone", "purpose")
    )
    to_create: list = []
    for c in Client.objects.only("id", "phone", "whatsapp_phone").iterator(
        chunk_size=500
    ):
        p = _norm(c.phone)
        if p and (p, "primary") not in taken:
            to_create.append(ClientPhone(client_id=c.id, phone=p, purpose="primary"))
            taken.add((p, "primary"))
        w = _norm(c.whatsapp_phone)
        if w and (w, "whatsapp") not in taken:
            to_create.append(ClientPhone(client_id=c.id, phone=w, purpose="whatsapp"))
            taken.add((w, "whatsapp"))

    ClientPhone.objects.bulk_create(to_create, batch_size=1000)
    print(f"ClientPhone backfill: создано {len(to_create)} записей")


def backward(apps, schema_editor):
    apps.get_model("crm", "ClientPhone").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0064_backfill_client_phones"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
