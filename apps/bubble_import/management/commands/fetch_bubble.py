"""Постраничная выгрузка сущности Bubble в staging.

  python manage.py fetch_bubble Man            # одна порция (50)
  python manage.py fetch_bubble Man --all      # выкачать всё
  python manage.py fetch_bubble Man --batch 200
"""
from django.core.management.base import BaseCommand, CommandError

from apps.bubble_import import bubble_api
from apps.bubble_import.models import ENTITY_CHOICES
from apps.bubble_import.services import fetch_batch, DEFAULT_BATCH, get_state

VALID = {e[0] for e in ENTITY_CHOICES}


class Command(BaseCommand):
    help = "Выгрузка данных из Bubble.io в staging-таблицу BubbleRecord"

    def add_arguments(self, parser):
        parser.add_argument("entity", choices=sorted(VALID))
        parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
        parser.add_argument("--all", action="store_true", help="Выкачать все страницы")

    def handle(self, *args, **opts):
        if not bubble_api.is_configured():
            raise CommandError("BUBBLE_API_TOKEN не задан")
        entity = opts["entity"]
        batch = opts["batch"]

        while True:
            res = fetch_batch(entity, batch=batch)
            self.stdout.write(
                f"  +{res['created']} новых, {res['updated']} обновлено, "
                f"всего в staging: {res['total_fetched']}/{res['total']}"
            )
            if not opts["all"]:
                break
            if res["remaining"] <= 0 or res["fetched"] == 0:
                break

        state = get_state(entity)
        self.stdout.write(self.style.SUCCESS(
            f"Готово. {entity}: {state.total_fetched}/{state.total_remote}"
        ))
