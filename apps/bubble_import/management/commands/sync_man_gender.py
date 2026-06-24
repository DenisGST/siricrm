"""Целевая досинхронизация Bubble Man.Пол → Client.gender.

Часть Client.gender не была заполнена при первом импорте — эта команда
проходит по всем BubbleRecord(Man) и проставляет gender для соответствующего
Client (по bubble_id), не запуская тяжёлый reapply всего Man.

  python manage.py sync_man_gender
  python manage.py sync_man_gender --dry-run
  python manage.py sync_man_gender --force   # перезапись имеющихся gender
"""
from collections import Counter

from django.core.management.base import BaseCommand

from apps.bubble_import.extractors import gender_from_bubble
from apps.bubble_import.models import BubbleRecord
from apps.crm.models import Client


class Command(BaseCommand):
    help = "Sync Bubble Man.Пол → Client.gender."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Не сохранять — только посчитать.")
        parser.add_argument("--force", action="store_true",
                            help="Перезаписать gender даже если он уже стоит.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        force = opts["force"]

        recs = BubbleRecord.objects.filter(entity="Man", status="imported")
        by_bubble = {c.bubble_id: c for c in Client.objects.exclude(bubble_id=None)}

        stats = Counter()
        for rec in recs:
            raw = rec.raw or {}
            bubble_gender = raw.get("Пол")
            if not bubble_gender:
                stats["no_pol_in_bubble"] += 1
                continue
            new_gender = gender_from_bubble(bubble_gender)
            if not new_gender:
                stats["unknown_pol_value"] += 1
                continue
            client = by_bubble.get(rec.bubble_id)
            if not client:
                stats["no_client"] += 1
                continue
            if client.gender == new_gender:
                stats["already_set"] += 1
                continue
            if client.gender and not force:
                stats["other_value_skipped"] += 1
                continue
            if not dry:
                client.gender = new_gender
                client.save(update_fields=["gender"])
            stats["updated"] += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== ИТОГ ==="))
        for k in ("updated", "already_set", "other_value_skipped",
                  "no_pol_in_bubble", "unknown_pol_value", "no_client"):
            self.stdout.write(f"  {k}: {stats[k]}")
