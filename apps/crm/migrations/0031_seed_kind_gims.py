from django.db import migrations


def seed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.update_or_create(
        name="Государственная инспекция по маломерным судам",
        defaults={"short_name": "ГИМС"},
    )


def unseed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.filter(short_name="ГИМС").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0030_seed_kind_mreo"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
