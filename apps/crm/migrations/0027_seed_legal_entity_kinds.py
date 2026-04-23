from django.db import migrations


SEED = [
    ("Банк", "Банк"),
    ("Микрофинансовая организация", "МФО"),
    ("Саморегулируемая организация", "СРО"),
    ("Коммерческая организация", "КО"),
    ("Федеральная налоговая служба", "ФНС"),
    ("Федеральная служба судебных приставов", "ФССП"),
]


def seed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    for name, short in SEED:
        Kind.objects.update_or_create(name=name, defaults={"short_name": short})


def unseed(apps, schema_editor):
    Kind = apps.get_model("crm", "LegalEntityKind")
    Kind.objects.filter(name__in=[n for n, _ in SEED]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0026_legalentitykind_legalentity_kind"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
