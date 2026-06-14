"""Сид ActionType для кнопки «Ознакомлен» уведомления."""
from django.db import migrations


def seed(apps, schema_editor):
    ActionType = apps.get_model("crm", "ActionType")
    ActionType.objects.update_or_create(
        code="notif_acknowledged",
        defaults=dict(
            name="Ознакомился с уведомлением",
            is_system=True, is_manual=False, notifies=False, order=904,
        ),
    )


def unseed(apps, schema_editor):
    ActionType = apps.get_model("crm", "ActionType")
    ActionType.objects.filter(code="notif_acknowledged").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0084_seed_notification_response_actions"),
    ]
    operations = [
        migrations.RunPython(seed, unseed),
    ]
