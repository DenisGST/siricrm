"""Сид справочника групп входящих и типа события «Пропущенный звонок».

Группы отражают ветки обзвона в диалплане АТС (`miss_call_cc` / `_osd` /
`_yuro`), а коды совпадают с тем, что присылает скрипт уведомления, — иначе
CRM не поймёт, куда шёл звонок.

🛑 Отделы НЕ привязываем автоматически: названия отделов на проде и на dev
свои, а угаданная привязка молча увела бы уведомления не тем людям. После
выкатки отдел у каждой группы выбирается в справочнике руками — до этого
уведомления уходят руководству (fallback в ``missed.recipients``).
"""
from django.db import migrations

GROUPS = [
    # (code, name, extensions)
    ("cc",   "Колл-центр",             "201,202,301,302"),
    ("osd",  "Отдел сбора документов", "401,402,403"),
    ("yuro", "Юридический отдел",      "501,502,503"),
]


def seed(apps, schema_editor):
    CallGroup = apps.get_model("telephony", "CallGroup")
    for code, name, extensions in GROUPS:
        CallGroup.objects.update_or_create(
            code=code,
            defaults={"name": name, "extensions": extensions, "is_active": True},
        )

    # Событие для лога клиента: пропущенный от известного клиента виден не
    # только дежурному по группе, но и тому, кто ведёт этого клиента.
    EventType = apps.get_model("crm", "EventType")
    EventType.objects.update_or_create(
        code="call_missed",
        defaults={
            "name": "Пропущенный звонок клиента",
            "source": "client",
            "order": 45,          # рядом с «Входящий звонок» (40)
            "is_system": True,
            "description": "Клиент звонил, но никто не ответил (или он оставил "
                           "голосовое сообщение). Пишется реестром пропущенных.",
        },
    )


def unseed(apps, schema_editor):
    apps.get_model("telephony", "CallGroup").objects.filter(
        code__in=[c for c, _n, _e in GROUPS]).delete()
    apps.get_model("crm", "EventType").objects.filter(code="call_missed").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("telephony", "0006_alter_calllisten_call_callgroup_missedcall_and_more"),
        ("crm", "0071_seed_and_migrate_log"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
