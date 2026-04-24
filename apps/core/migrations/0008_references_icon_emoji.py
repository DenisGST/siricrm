from django.db import migrations


def set_icon(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    MenuItem.objects.filter(url="/references/").update(icon="📚")


def unset_icon(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    MenuItem.objects.filter(url="/references/").update(icon="book-text")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0007_menuitem_requires_elevated_and_seed_refs"),
    ]

    operations = [
        migrations.RunPython(set_icon, unset_icon),
    ]
