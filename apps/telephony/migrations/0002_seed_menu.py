"""Пункт меню «Звонки» (/telephony/).

Идемпотентно. Видимость регулируется в apps.core.context_processors по
can_access_calls — как у «Отчётов», «Бухучёта» и «Юриста БФЛ».
"""
from django.db import migrations


def seed(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")

    item, _ = MenuItem.objects.get_or_create(
        url="/telephony/",
        defaults={
            "name": "Звонки",
            "icon": "phone",
            "section": "Инструменты",
            "order": 59,
            "use_htmx": True,
            "requires_elevated": False,
            "is_active": True,
        },
    )
    for cfg in DashboardConfig.objects.filter(is_active=True):
        cfg.menu_items.add(item)


def unseed(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("telephony", "0001_initial"),
        ("core", "0037_employee_can_listen_calls_employee_sip_extension"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
