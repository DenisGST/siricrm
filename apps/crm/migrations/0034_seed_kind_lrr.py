from django.db import migrations


def seed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.update_or_create(
        name="Подразделение лицензионно-разрешительной работы Росгвардии",
        defaults={"short_name": "ЛРР"},
    )


def unseed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.filter(short_name="ЛРР").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0033_legalentity_region"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
