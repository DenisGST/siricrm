"""Доливка из Bubble: записи, изменённые с указанной даты.

  python manage.py fetch_bubble_since 2026-05-22
  python manage.py fetch_bubble_since 7              # последние 7 дней
  python manage.py fetch_bubble_since 7 --apply      # сразу прогнать apply

Использует фильтр `Modified Date > since` — попадают и новые, и
обновлённые в Bubble записи. По умолчанию проходит все основные
сущности (User, Man, ProjectBFL, Money, MessageWSP, Files). Помечает
новые и изменённые записи `approved=True, status='pending'` чтобы они
ушли в apply.
"""
import datetime
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.bubble_import.models import BubbleRecord
from apps.bubble_import.services import fetch_modified_since

logger = logging.getLogger("bubble_import")

DEFAULT_ENTITIES = ["User", "Man", "ProjectBFL", "Money", "MessageWSP", "Files"]


class Command(BaseCommand):
    help = "Доливка изменений из Bubble по Modified Date > since"

    def add_arguments(self, parser):
        parser.add_argument(
            "since",
            help="Дата YYYY-MM-DD или число дней назад (например, 7)",
        )
        parser.add_argument(
            "--entities", default=",".join(DEFAULT_ENTITIES),
            help=f"Через запятую. По умолчанию: {','.join(DEFAULT_ENTITIES)}",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="После fetch прогнать apply_approved для каждой сущности",
        )

    def handle(self, *args, **opts):
        raw = opts["since"]
        if raw.isdigit():
            since = timezone.now() - datetime.timedelta(days=int(raw))
        else:
            since = datetime.datetime.fromisoformat(raw)
            if timezone.is_naive(since):
                since = timezone.make_aware(since)

        entities = [e.strip() for e in opts["entities"].split(",") if e.strip()]
        self.stdout.write(f"Доливка с {since.isoformat()} ({len(entities)} сущностей)\n")

        totals = {}
        for entity in entities:
            self.stdout.write(f"\n=== {entity} ===")
            try:
                res = fetch_modified_since(entity, since)
            except Exception as e:  # noqa: BLE001
                self.stderr.write(f"  ОШИБКА: {e}")
                continue
            n_new = res["created"]; n_upd = res["updated"]
            self.stdout.write(f"  fetched: {n_new} new, {n_upd} updated")
            # Одобряем всё новое + изменённое, чтобы apply подхватил.
            n_appr = BubbleRecord.objects.filter(
                entity=entity, approved=False,
            ).update(approved=True)
            self.stdout.write(f"  одобрено новых: {n_appr}")
            totals[entity] = {"new": n_new, "upd": n_upd}

        if opts["apply"]:
            self.stdout.write("\n=== APPLY ===")
            from apps.bubble_import.appliers import apply_approved
            for entity in entities:
                self.stdout.write(f"\n--- apply {entity} ---")
                try:
                    res = apply_approved(entity)
                    self.stdout.write(
                        f"  {entity}: импорт {res.get('imported')}, "
                        f"ошибки {res.get('errors')}"
                        + (f", связано супругов {res['spouses_linked']}"
                           if "spouses_linked" in res else "")
                    )
                except Exception as e:  # noqa: BLE001
                    self.stderr.write(f"  ОШИБКА apply {entity}: {e}")

        self.stdout.write(self.style.SUCCESS(
            "\nГотово. Итого: " + ", ".join(
                f"{e}: +{t['new']}/upd {t['upd']}" for e, t in totals.items()
            )
        ))
