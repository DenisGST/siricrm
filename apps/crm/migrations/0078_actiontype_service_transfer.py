"""ActionType «Передать в работу» — для лога передачи услуги в отдел/сотруднику."""
from django.db import migrations


def seed(apps, schema_editor):
    ActionType = apps.get_model("crm", "ActionType")
    ActionType.objects.update_or_create(
        code="service_transfer",
        defaults={
            "name": "Передать в работу",
            "description": "Услуга передана в работу отдела или конкретного сотрудника.",
            "is_system": True,   # защищаем от удаления
            "is_manual": False,  # выполняется кнопкой в модалке услуги, не через ручной лог
            "is_active": True,
            "order": 0,
        },
    )


def unseed(apps, schema_editor):
    ActionType = apps.get_model("crm", "ActionType")
    ActionType.objects.filter(code="service_transfer").delete()


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0077_inbox_unique_constraint'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
