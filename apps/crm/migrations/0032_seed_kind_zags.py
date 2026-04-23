from django.db import migrations


def seed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.update_or_create(
        name="Орган записи актов гражданского состояния",
        defaults={"short_name": "ЗАГС"},
    )


def unseed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.filter(short_name="ЗАГС").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0031_seed_kind_gims"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
