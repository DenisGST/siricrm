from django.db import migrations


def seed(apps, schema_editor):
    """Пункт меню «Импорт из Bubble» — только для суперпользователя."""
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")
    mi, _ = MenuItem.objects.update_or_create(
        url="/imports/bubble/",
        defaults={
            "name": "Импорт из Bubble",
            "icon": "download",
            "section": "Администрирование",
            "order": 92,
            "use_htmx": True,
            "requires_superuser": True,
            "requires_elevated": False,
            "is_active": True,
        },
    )
    for dc in DashboardConfig.objects.all():
        dc.menu_items.add(mi)


def unseed(apps, schema_editor):
    apps.get_model("core", "MenuItem").objects.filter(url="/imports/bubble/").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("bubble_import", "0001_initial"),
        ("core", "0012_alter_employee_role"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
