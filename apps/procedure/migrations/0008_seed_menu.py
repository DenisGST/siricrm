"""Сидинг пункта меню «Юрист БФЛ» (/procedure/).

Идемпотентно. Пункт заведён без requires_elevated — видимость регулируется
вручную в apps.core.context_processors (по can_access_procedures), как у
«Бухгалтерского учёта» и «Входящих сканов».
"""
from django.db import migrations


def seed(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")

    item, _ = MenuItem.objects.get_or_create(
        url="/procedure/",
        defaults={
            "name": "Юрист БФЛ",
            "icon": "femida",
            "section": "Инструменты",
            "order": 56,
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
        ("procedure", "0007_remove_procedure_publication_date_and_more"),
        ("core", "0024_department_is_docs_collection"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
