from django.db import migrations, models


def seed_references_menu(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")
    mi, _ = MenuItem.objects.update_or_create(
        url="/references/",
        defaults={
            "name": "Справочники",
            "icon": "📚",
            "section": "Администрирование",
            "order": 93,
            "use_htmx": True,
            "requires_superuser": False,
            "requires_elevated": True,
            "is_active": True,
        },
    )
    # Добавляем во все активные конфигурации дашбордов.
    for dc in DashboardConfig.objects.all():
        dc.menu_items.add(mi)


def unseed_references_menu(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    MenuItem.objects.filter(url="/references/").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_menuitem_icon_50"),
    ]

    operations = [
        migrations.AddField(
            model_name="menuitem",
            name="requires_elevated",
            field=models.BooleanField(
                default=False,
                help_text="Видим только superuser / admin / head_dep",
                verbose_name="Только для администраторов и руководителей",
            ),
        ),
        migrations.RunPython(seed_references_menu, unseed_references_menu),
    ]
