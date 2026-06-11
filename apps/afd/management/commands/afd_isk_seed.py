"""Идемпотентный сидинг шаблона заявления о банкротстве (разделы).

Запуск:  python manage.py afd_isk_seed
"""
from django.core.management.base import BaseCommand

from apps.afd.isk_seed_data import DEFAULT_TEMPLATE_NAME, SECTIONS
from apps.afd.models import IskSection, IskTemplate


class Command(BaseCommand):
    help = "Сидинг шаблона заявления о банкротстве (IskTemplate + разделы)"

    def handle(self, *args, **opts):
        tpl, created = IskTemplate.objects.get_or_create(
            name=DEFAULT_TEMPLATE_NAME,
            defaults={"is_default": True, "is_active": True},
        )
        if not created and tpl.sections.exists():
            self.stdout.write("• Шаблон заявления уже есть с разделами — пропуск.")
            return
        for i, s in enumerate(SECTIONS):
            IskSection.objects.create(
                template=tpl, order=(i + 1) * 10,
                key=s.get("key", ""), title=s.get("title", ""),
                body=s.get("body", ""), block_type=s.get("block_type", "text"),
                align=s.get("align", "both"), bold=s.get("bold", False),
                is_optional=s.get("is_optional", False),
                include_condition=s.get("include_condition", ""),
            )
        self.stdout.write(self.style.SUCCESS(
            f"• Шаблон «{tpl.name}» + {len(SECTIONS)} разделов засидены."
        ))
