"""Целевая досинхронизация супружеских связей из Bubble.

Bubble Man содержит поля « isMarried» и « spouse» (баг — с пробелом
в начале ключа). Эта команда обновляет Client.is_married и Client.spouse
для всех затронутых записей, не запуская массовый reapply Man.

  python manage.py sync_spouses
  python manage.py sync_spouses --dry-run
"""
from collections import Counter

from django.core.management.base import BaseCommand

from apps.bubble_import.models import BubbleRecord
from apps.crm.models import Client


class Command(BaseCommand):
    help = "Синхронизация Bubble Man → Client.is_married + Client.spouse."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Не сохранять — только посчитать.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]

        # Все Man (имеют любой признак брака)
        recs = list(BubbleRecord.objects.filter(entity="Man", status="imported"))
        self.stdout.write(f"Bubble Man imported: {len(recs)}")

        by_bubble = {c.bubble_id: c for c in Client.objects.exclude(bubble_id=None)}
        self.stdout.write(f"Client с bubble_id: {len(by_bubble)}")

        stats = Counter()

        for rec in recs:
            raw = rec.raw or {}
            # ⚠ Bubble-баг: ключ с пробелом.
            is_married_raw = raw.get("isMarried") or raw.get(" isMarried")
            spouse_bid = raw.get("spouse") or raw.get(" spouse")

            client = by_bubble.get(rec.bubble_id)
            if not client:
                stats["no_client"] += 1
                continue

            # is_married
            new_is_married = bool(is_married_raw)
            if client.is_married != new_is_married:
                if not dry:
                    client.is_married = new_is_married
                    client.save(update_fields=["is_married"])
                stats["is_married_updated"] += 1

            # spouse FK
            if spouse_bid:
                spouse = by_bubble.get(spouse_bid)
                if not spouse:
                    stats["spouse_not_in_siri"] += 1
                    continue
                # прямая связь A → B
                if client.spouse_id != spouse.id:
                    if not dry:
                        client.spouse = spouse
                        client.save(update_fields=["spouse"])
                    stats["spouse_set"] += 1
                # зеркало B → A (Bubble хранит связь только с одной стороны)
                if spouse.spouse_id != client.id:
                    if not dry:
                        spouse.spouse = client
                        spouse.is_married = True
                        spouse.save(update_fields=["spouse", "is_married"])
                    stats["spouse_mirrored"] += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== ИТОГ ==="))
        for k in ("is_married_updated", "spouse_set", "spouse_mirrored",
                  "spouse_not_in_siri", "no_client"):
            self.stdout.write(f"  {k}: {stats[k]}")
