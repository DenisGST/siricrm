"""Стартовое наполнение: пункт меню «Колл-центр» и черновой набор колонок.

Идемпотентно (get_or_create). Колонки — ЧЕРНОВИК: их состав и названия
администратор правит в Панели управления → «Колл-центр», код после этого
не трогаем.
"""
from django.db import migrations

COLUMNS = [
    ("Новые обращения", "Ещё не звонили", "info", 10, True),
    ("Недозвон", "Не берут трубку — повторить попытку", "warning", 20, False),
    ("Перезвонить", "Договорились о времени звонка", "primary", 30, False),
    ("Назначена консультация", "Клиент записан на встречу", "success", 40, False),
    ("Отказ", "Не заинтересован", "error", 50, False),
]


def seed(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")
    Column = apps.get_model("callcenter", "CallCenterColumn")

    item, _ = MenuItem.objects.get_or_create(
        url="/callcenter/",
        defaults={
            "name": "Колл-центр",
            "icon": "kanban-square",
            "section": "Инструменты",
            "order": 57,
            "use_htmx": True,
            # Видимость гейтится вручную в apps.core.context_processors
            # (can_access_callcenter) — как у «Отчётов» и «Звонков».
            "requires_elevated": False,
            "is_active": True,
        },
    )
    for cfg in DashboardConfig.objects.filter(is_active=True):
        cfg.menu_items.add(item)

    for name, hint, color, order, is_default in COLUMNS:
        Column.objects.get_or_create(
            name=name,
            defaults={
                "description": hint, "color": color, "order": order,
                "is_default": is_default, "is_active": True, "wip_limit": 0,
            },
        )


def unseed(apps, schema_editor):
    # Откат не трогает данные: колонки могли уже наполниться карточками.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("callcenter", "0001_initial"),
        ("core", "0040_employee_can_access_callcenter"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
