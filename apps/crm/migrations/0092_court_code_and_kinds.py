"""LegalEntity.court_code + 2 LegalEntityKind для судов."""
from django.db import migrations, models


KINDS = [
    {"short_name": "Районный суд",
     "name": "Районный (городской, межрайонный) суд общей юрисдикции"},
    {"short_name": "Мировой участок",
     "name": "Судебный участок мирового судьи"},
]


def add_kinds(apps, schema_editor):
    LegalEntityKind = apps.get_model("crm", "LegalEntityKind")
    for k in KINDS:
        LegalEntityKind.objects.update_or_create(
            short_name=k["short_name"], defaults={"name": k["name"]},
        )


def remove_kinds(apps, schema_editor):
    LegalEntityKind = apps.get_model("crm", "LegalEntityKind")
    LegalEntityKind.objects.filter(
        short_name__in=[k["short_name"] for k in KINDS],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0091_region_kherson'),
    ]

    operations = [
        migrations.AddField(
            model_name='legalentity',
            name='court_code',
            field=models.CharField(blank=True, help_text='Код суда формата «22RS0001» (2 цифры — код субъекта РФ, 2 буквы — тип, 4 цифры — порядковый). Для идемпотентного импорта из sudrf.ru.', max_length=20, null=True, unique=True, verbose_name='Код суда (ГАС Правосудие)'),
        ),
        migrations.RunPython(add_kinds, remove_kinds),
    ]
