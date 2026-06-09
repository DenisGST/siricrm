"""
Добавляет в справочник событийки тип события «Договор полностью оплачен»
(code=contract_fully_paid) — доступен для ручного добавления в логе клиента.
Идемпотентно через update_or_create.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    EventType = apps.get_model("crm", "EventType")
    EventType.objects.update_or_create(
        code="contract_fully_paid",
        defaults=dict(
            name="Договор полностью оплачен",
            source="client",
            order=50,
            is_system=False,
            is_manual=True,
            is_active=True,
            description="Клиент полностью оплатил договор юридических услуг.",
        ),
    )


def backwards(apps, schema_editor):
    EventType = apps.get_model("crm", "EventType")
    EventType.objects.filter(code="contract_fully_paid").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0080_message_error_text_message_is_failed"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
