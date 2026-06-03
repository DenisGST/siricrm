"""Привязывает MenuItem /arbitr/ ко всем DashboardConfig'ам.

Sidebar в `context_processors.sidebar_menu` рендерит только те пункты,
которые лежат в `Employee.dashboard_config.menu_items` (M2M). Без этой
привязки пункт «Арбитраж» в меню не появится, даже если is_active=True.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")

    arbitr = MenuItem.objects.filter(url="/arbitr/").first()
    if not arbitr:
        return  # 0016 не применилась (или меню переименовали) — выходим тихо
    for cfg in DashboardConfig.objects.all():
        cfg.menu_items.add(arbitr)


def backwards(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")
    arbitr = MenuItem.objects.filter(url="/arbitr/").first()
    if not arbitr:
        return
    for cfg in DashboardConfig.objects.all():
        cfg.menu_items.remove(arbitr)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_arbitr_menu_item"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
