"""
Включает can_merge_clients для тех, кому положено объединять карточки:
суперпользователи (Каныгин) + сотрудник Власов. Остальным — выдаётся вручную
в карточке сотрудника. Идемпотентно.
"""
from django.db import migrations
from django.db.models import Q


def forwards(apps, schema_editor):
    Employee = apps.get_model("core", "Employee")
    Employee.objects.filter(
        Q(user__is_superuser=True) | Q(user__last_name__icontains="Власов")
    ).update(can_merge_clients=True)


def backwards(apps, schema_editor):
    Employee = apps.get_model("core", "Employee")
    Employee.objects.update(can_merge_clients=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_employee_can_merge_clients"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
