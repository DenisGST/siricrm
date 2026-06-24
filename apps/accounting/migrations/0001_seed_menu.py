"""Сидинг пункта меню «Бухгалтерский учёт» (/accounting/).

Идемпотентно. Пункт заведён без requires_elevated — видимость регулируется
вручную в apps.core.context_processors (по can_access_accounting), как у
«Входящих сканов»: бухгалтеры не относятся к elevated-ролям, но раздел им нужен.
"""
from django.db import migrations


def seed(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")

    item, _ = MenuItem.objects.get_or_create(
        url="/accounting/",
        defaults={
            "name": "Бухгалтерский учёт",
            "icon": "calculator",
            "section": "Инструменты",
            "order": 55,
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
    dependencies = [
        ("core", "0022_set_can_merge_clients"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
