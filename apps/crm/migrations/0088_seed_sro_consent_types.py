"""
Справочник событийки: действие «Отправлено согласие в СРО» (ActionType
sro_consent_sent) + событие «В дело поступило согласие от СРО» (EventType
sro_consent_received). Оба доступны для ручного добавления. Идемпотентно.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    EventType = apps.get_model("crm", "EventType")
    ActionType = apps.get_model("crm", "ActionType")

    EventType.objects.update_or_create(
        code="sro_consent_received",
        defaults=dict(
            name="В дело поступило согласие от СРО",
            source="legal_entity",
            order=60,
            is_system=False,
            is_manual=True,
            is_active=True,
            description="От СРО получено согласие на утверждение арбитражного управляющего.",
        ),
    )
    ActionType.objects.update_or_create(
        code="sro_consent_sent",
        defaults=dict(
            name="Отправлено согласие в СРО",
            order=60,
            is_system=False,
            is_manual=True,
            is_active=True,
            description="В СРО направлен запрос/согласие по делу о банкротстве.",
        ),
    )


def backwards(apps, schema_editor):
    apps.get_model("crm", "EventType").objects.filter(code="sro_consent_received").delete()
    apps.get_model("crm", "ActionType").objects.filter(code="sro_consent_sent").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0087_service_docs_dept_date"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
