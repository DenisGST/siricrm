"""85 региональных управлений Росреестра в LegalEntity.

Адресат «Информационного письма в Росреестр» (по образцу юриста: «Управление
Росреестра по Ставропольскому краю, г. Ставрополь, ул. Комсомольская, 58»).
Не путать с «Запросом в Росреестр» — тот идёт через СМЭВ и адресата не требует.

Данные собраны на dev командой `import_rosreestr` (падежная форма «по <региону>»
берётся из названий управлений ФССП, реквизиты — DaData) и сохранены в
apps/crm/data/rosreestr_regional.json. 🛑 Здесь грузим ИЗ ФАЙЛА, не из DaData:
квота общая на ключ, а миграция должна быть детерминированной.

Идемпотентно: update_or_create по (вид «Росреестр» + регион).
"""
import json
from pathlib import Path

from django.db import migrations

DATA = Path(__file__).resolve().parent.parent / "data" / "rosreestr_regional.json"
KIND_NAME = "Управление Росреестра по субъекту РФ"
KIND_SHORT = "Росреестр"


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
    for row in json.loads(DATA.read_text(encoding="utf-8")):
        region = regions.get(row["region"])
        if region is None:
            continue
        LegalEntity.objects.update_or_create(
            kind=kind, region=region,
            defaults={
                "name": row["name"], "short_name": row["short_name"],
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
        ("crm", "0099_seed_ufssp_regional"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
