"""Даём флаг can_view_all_clients Каныгиной Светлане Ивановне.

На проде её реальная роль — `arbitration` (Арбитражный управляющий),
которая по visible_to не даёт доступ ко всем клиентам (даёт только
managing_partner/head_dep/accountant). Меняем не роль (она реально АУ
и должна оставаться в этой роли для apps/procedure), а даём точечный флаг.

Идемпотентно: если Каныгина не найдена — no-op с warning.
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
        print("  ⚠ Каныгина Светлана не найдена — пропускаю")
        return
    if n > 1:
        print(f"  ⚠ найдено {n} записей — включаю всем")
    qs.update(can_view_all_clients=True)
    for e in qs:
        print(f"  ✓ Employee id={e.id} ({e.user.last_name} {e.user.first_name}): can_view_all_clients=True")


def _revoke(apps, schema_editor):
    Employee = apps.get_model("core", "Employee")
    Employee.objects.filter(
        user__last_name="Каныгина",
        user__first_name="Светлана",
    ).update(can_view_all_clients=False)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0029_employee_can_view_all_clients"),
    ]

    operations = [
        migrations.RunPython(_grant, _revoke),
    ]
