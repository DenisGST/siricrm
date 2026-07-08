"""Даём флаг can_edit_finance Каныгиной Светлане Ивановне (managing_partner).

По просьбе от 06.07.2026: доступ на внесение и изменение платежей
(входящие/исходящие) без смены роли. Точечно, идемпотентно.

Ищем по user.last_name='Каныгина' + user.first_name='Светлана' —
если найдено более одной или ноль, no-op с warning в stdout миграции.
"""
from django.db import migrations


def _grant(apps, schema_editor):
    Employee = apps.get_model("core", "Employee")
    qs = Employee.objects.filter(
        user__last_name="Каныгина",
        user__first_name="Светлана",
    )
    n = qs.count()
    if n == 0:
        print("  ⚠ Каныгина Светлана не найдена — пропускаю (создастся вручную позже)")
        return
    if n > 1:
        print(f"  ⚠ найдено {n} записей 'Каныгина Светлана' — включаю всем")
    qs.update(can_edit_finance=True)
    for e in qs:
        print(f"  ✓ Employee id={e.id} ({e.user.last_name} {e.user.first_name}): can_edit_finance=True")


def _revoke(apps, schema_editor):
    Employee = apps.get_model("core", "Employee")
    Employee.objects.filter(
        user__last_name="Каныгина",
        user__first_name="Светлана",
    ).update(can_edit_finance=False)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_employee_can_edit_finance"),
    ]

    operations = [
        migrations.RunPython(_grant, _revoke),
    ]
