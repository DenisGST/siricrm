"""Импорт одобренных staging-записей в продакшн-модели SiriCRM.

  python manage.py apply_bubble Man      # импортировать одобренные Man → Client
"""
from django.core.management.base import BaseCommand

from apps.bubble_import.appliers import apply_approved
from apps.bubble_import.models import ENTITY_CHOICES

VALID = {e[0] for e in ENTITY_CHOICES}


class Command(BaseCommand):
    help = "Импорт одобренных записей Bubble в модели SiriCRM"

    def add_arguments(self, parser):
        parser.add_argument("entity", choices=sorted(VALID))

    def handle(self, *args, **opts):
        res = apply_approved(opts["entity"])
        self.stdout.write(self.style.SUCCESS(
            f"{opts['entity']}: импортировано {res['imported']}, "
            f"ошибок {res['errors']}"
            + (f", связано супругов {res['spouses_linked']}"
               if "spouses_linked" in res else "")
        ))
