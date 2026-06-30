"""Сидинг пункта меню «Отчёты» (/reports/).

Идемпотентно. Пункт заведён без requires_elevated — видимость регулируется
вручную в apps.core.context_processors (по can_access_reports), как у
«Бухгалтерского учёта», «Входящих сканов» и «Юриста БФЛ».
"""
from django.db import migrations


def seed(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")

    item, _ = MenuItem.objects.get_or_create(
        url="/reports/",
        defaults={
            "name": "Отчёты",
            "icon": "bar-chart-3",
            "section": "Инструменты",
            "order": 58,
            "use_htmx": True,
            "requires_elevated": False,
            "is_active": True,
        },
    )
    for cfg in DashboardConfig.objects.filter(is_active=True):
        cfg.menu_items.add(item)


def unseed(apps, schema_editor):
    # Откат не трогает данные.
    pass


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("core", "0026_fix_daily_report_queue"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
