from django.db import migrations


def seed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.update_or_create(
        name="Межрайонный регистрационно-экзаменационный отдел ГИБДД",
        defaults={"short_name": "МРЭО"},
    )


def unseed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.filter(short_name="МРЭО").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0029_seed_kind_collectors"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
