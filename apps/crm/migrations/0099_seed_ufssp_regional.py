"""85 региональных управлений ФССП (У/ГУ ФССП России по субъекту) в LegalEntity.

Нужны как адресат «Информационного письма в УФССП»: в справочнике до этого были
только районные ОСП (вид «ФССП»), а письмо адресуется управлению субъекта.

Данные собраны на dev командой `import_ufssp` (имя управления берётся из названий
самих ОСП — там оно уже в правильном падеже; реквизиты — DaData) и сохранены в
apps/crm/data/ufssp_regional.json. 🛑 Здесь грузим ИЗ ФАЙЛА, а не из DaData:
квота DaData общая на ключ (парсим только на dev, см. память
external-api-dev-then-sync), а миграция должна быть детерминированной.

Идемпотентно: update_or_create по (вид «УФССП» + регион).
"""
import json
from pathlib import Path

from django.db import migrations

DATA = Path(__file__).resolve().parent.parent / "data" / "ufssp_regional.json"
KIND_NAME = "Управление ФССП России по субъекту РФ (региональное)"
KIND_SHORT = "УФССП"


def forwards(apps, schema_editor):
    LegalEntity = apps.get_model("crm", "LegalEntity")
    LegalEntityKind = apps.get_model("crm", "LegalEntityKind")
    Region = apps.get_model("crm", "Region")

    if not DATA.exists():
        return
    kind, _ = LegalEntityKind.objects.get_or_create(
        name=KIND_NAME, defaults={"short_name": KIND_SHORT},
    )
    regions = {r.number: r for r in Region.objects.all()}
    rows = json.loads(DATA.read_text(encoding="utf-8"))
    for row in rows:
        region = regions.get(row["region"])
        if region is None:
            continue
        LegalEntity.objects.update_or_create(
            kind=kind, region=region,
            defaults={
                "name": row["name"],
                "short_name": row["short_name"],
                "inn": row["inn"], "kpp": row["kpp"], "ogrn": row["ogrn"],
                "legal_address": row["legal_address"],
                "postal_code": row["postal_code"],
                "is_active": True,
            },
        )


def backwards(apps, schema_editor):
    LegalEntity = apps.get_model("crm", "LegalEntity")
    LegalEntityKind = apps.get_model("crm", "LegalEntityKind")
    kind = LegalEntityKind.objects.filter(name=KIND_NAME).first()
    if kind:
        LegalEntity.objects.filter(kind=kind).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0098_legalentity_postal_code"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
