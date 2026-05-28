"""Добавляет пункт меню для сервисной страницы арбитража."""
from django.db import migrations


def forwards(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    MenuItem.objects.update_or_create(
        url="/arbitr/",
        defaults={
            "name": "Мониторинг дел",
            "section": "Арбитраж",
            "icon": "landmark",  # суд (здание с колоннами); 'scale' нет в static/icons/line/
            "order": 10,
            "use_htmx": False,        # это самостоятельная страница, не HTMX-партиал дашборда
            "requires_elevated": True, # admin/management — для отладки и работы
            "is_active": True,
        },
    )


def backwards(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    MenuItem.objects.filter(url="/arbitr/").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_department_sees_all_clients_employee_is_owner"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
