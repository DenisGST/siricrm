"""Идемпотентный сидинг пункта меню «Входящие сканы».

Видимость пункта по флагу Employee.can_handle_scans обеспечивает
context_processor (sidebar_menu) — здесь пункт заводится без requires_*.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Создаёт пункт меню «Входящие сканы»."

    def handle(self, *args, **options):
        from apps.core.models import DashboardConfig, MenuItem

        item, created = MenuItem.objects.get_or_create(
            url="/scans/",
            defaults={
                "name": "Входящие сканы",
                "icon": "scan",
                "section": "Инструменты",
                "order": 55,
                "use_htmx": True,
                "requires_elevated": False,
                "requires_superuser": False,
                "is_active": True,
            },
        )
        for cfg in DashboardConfig.objects.filter(is_active=True):
            cfg.menu_items.add(item)
        self.stdout.write(
            "• Пункт меню «Входящие сканы» "
            + ("создан." if created else "уже есть.")
        )
