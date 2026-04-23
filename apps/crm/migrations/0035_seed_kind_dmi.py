from django.db import migrations


def seed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.update_or_create(
        name="Орган управления муниципальным имуществом",
        defaults={"short_name": "ДМИ"},
    )


def unseed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.filter(short_name="ДМИ").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0034_seed_kind_lrr"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
