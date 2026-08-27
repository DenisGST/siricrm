"""Назначить колонки-приёмники автонаполнения на засеянном наборе.

Оба источника ведут в «Новые обращения» — колонку, с которой начинается
обзвон. Идемпотентно и бережно: если админ уже назначил приёмник САМ
(неважно, какой колонке), ничего не трогаем.
"""
from django.db import migrations


def seed(apps, schema_editor):
    Column = apps.get_model("callcenter", "CallCenterColumn")
    target = Column.objects.filter(name="Новые обращения").first()
    if target is None:
        return
    for field in ("catch_unknown_calls", "catch_telegram_leads"):
        if Column.objects.filter(**{field: True}).exists():
            continue
        setattr(target, field, True)
    target.save(update_fields=["catch_unknown_calls", "catch_telegram_leads"])


def unseed(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("callcenter", "0003_callcentercard_source_and_more")]
    operations = [migrations.RunPython(seed, unseed)]
