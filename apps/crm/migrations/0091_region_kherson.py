"""Добавляем Херсонскую область (code 84) в справочник регионов.

В исходном Region уже есть ДНР (80), ЛНР (81), Крым (82), Запорожская (85),
а Херсонская обл (84) отсутствует — добавляется здесь для корректной привязки
LegalEntity (ОСП ФССП по Херсонской области)."""
from django.db import migrations


def add_kherson(apps, schema_editor):
    Region = apps.get_model("crm", "Region")
    Region.objects.update_or_create(
        number=84,
        defaults={
            "name": "Херсонская область",
            "court_name": "Арбитражный суд Херсонской области",
            "court_payment_details": "",  # реквизиты заполняются вручную при использовании
        },
    )


def remove_kherson(apps, schema_editor):
    Region = apps.get_model("crm", "Region")
    Region.objects.filter(number=84).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0090_legalentity_fssp_code"),
    ]

    operations = [
        migrations.RunPython(add_kherson, remove_kherson),
    ]
