from django.db import migrations


def seed(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")
    mi, _ = MenuItem.objects.update_or_create(
        url="/legal-entities/",
        defaults={
            "name": "Юридические лица",
            "icon": "building-2",
            "section": "CRM",
            "order": 14,
            "use_htmx": True,
            "requires_superuser": False,
            "requires_elevated": False,
            "is_active": True,
        },
    )
    for dc in DashboardConfig.objects.all():
        dc.menu_items.add(mi)


def unseed(apps, schema_editor):
    apps.get_model("core", "MenuItem").objects.filter(url="/legal-entities/").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_seed_menu_services_kanbans"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
