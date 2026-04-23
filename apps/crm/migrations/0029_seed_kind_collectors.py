from django.db import migrations


def seed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.update_or_create(
        name="Коллекторское агентство",
        defaults={"short_name": "КА"},
    )


def unseed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.filter(name="Коллекторское агентство").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0028_legalentity_brand"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
