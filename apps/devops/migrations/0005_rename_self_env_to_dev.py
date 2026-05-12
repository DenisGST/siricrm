"""Переименование Environment 'self' → 'dev' (понятнее в UI: разработка ведётся тут)."""
from django.db import migrations


def rename_forward(apps, schema_editor):
    Environment = apps.get_model("devops", "Environment")
    Environment.objects.filter(name="self").update(name="dev")


def rename_backward(apps, schema_editor):
    Environment = apps.get_model("devops", "Environment")
    Environment.objects.filter(name="dev").update(name="self")


class Migration(migrations.Migration):

    dependencies = [
        ("devops", "0004_alter_devopsaction_action_type"),
    ]

    operations = [
        migrations.RunPython(rename_forward, rename_backward),
    ]
