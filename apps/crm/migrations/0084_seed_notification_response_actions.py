"""Сид ActionType для записей-ответов на уведомления в событийке.

Кнопки уведомления (Принять/Исполнено/Отклонить/Отложить) пишут действие
этого типа через client_log.record_action (см. apps/notifications/services.py).
"""
from django.db import migrations

RESPONSE_ACTIONS = [
    ("notif_accepted", "Принял уведомление в работу"),
    ("notif_done",     "Отметил уведомление исполненным"),
    ("notif_rejected", "Отклонил уведомление"),
    ("notif_snoozed",  "Отложил уведомление"),
]


def seed(apps, schema_editor):
    ActionType = apps.get_model("crm", "ActionType")
    for order, (code, name) in enumerate(RESPONSE_ACTIONS, start=900):
        ActionType.objects.update_or_create(
            code=code,
            defaults=dict(
                name=name, is_system=True, is_manual=False,
                notifies=False, order=order,
            ),
        )


def unseed(apps, schema_editor):
    ActionType = apps.get_model("crm", "ActionType")
    ActionType.objects.filter(
        code__in=[c for c, _ in RESPONSE_ACTIONS]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0083_actiontype_notifies_actiontype_notify_hint_and_more"),
    ]
    operations = [
        migrations.RunPython(seed, unseed),
    ]
