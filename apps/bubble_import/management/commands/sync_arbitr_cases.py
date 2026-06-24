"""Импорт сведений об арбитражном деле из Bubble ProjectBFL в Siri ArbitrCase.

Поля в Bubble ProjectBFL:
  numbDelo       → ArbitrCase.case_number  (формат «А12-22421/2023»)
  linkKadArbitr  → ArbitrCase.kad_url      (https://kad.arbitr.ru/Card/<uuid>)

В Bubble ~1200 ProjectBFL содержат эти поля. Создаём ArbitrCase для каждой
импортированной Siri Service (ProjectBFL.bubble_id), статус — monitoring
(карточка уже найдена в Bubble — поиск пропускаем).

  python manage.py sync_arbitr_cases
  python manage.py sync_arbitr_cases --dry-run
  python manage.py sync_arbitr_cases --force   # перезапись имеющихся
"""
from collections import Counter

from django.core.management.base import BaseCommand

from apps.arbitr.models import ArbitrCase
from apps.bubble_import.models import BubbleRecord
from apps.crm.models import Service


class Command(BaseCommand):
    help = "Bubble ProjectBFL.numbDelo/linkKadArbitr → Siri ArbitrCase."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true",
                            help="Перезаписать case_number/kad_url если уже стоят.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        force = opts["force"]

        # Все ProjectBFL у кого есть хотя бы одно из 2-х полей.
        recs = BubbleRecord.objects.filter(entity="ProjectBFL", status="imported")
        services_by_bid = {
            s.bubble_id: s for s in Service.objects.exclude(bubble_id=None)
        }
        self.stdout.write(f"Bubble ProjectBFL imported: {recs.count()}")
        self.stdout.write(f"Siri Service с bubble_id: {len(services_by_bid)}")

        stats = Counter()
        for rec in recs:
            raw = rec.raw or {}
            case_number = (raw.get("numbDelo") or "").strip()
            kad_url = (raw.get("linkKadArbitr") or "").strip()
            if not case_number and not kad_url:
                continue  # нет данных о деле

            service = services_by_bid.get(rec.bubble_id)
            if service is None:
                stats["no_service"] += 1
                continue

            try:
                ac = service.arbitr_case  # OneToOne related access
            except ArbitrCase.DoesNotExist:
                ac = None

            if ac is None:
                if dry:
                    stats["would_create"] += 1
                    continue
                ArbitrCase.objects.create(
                    service=service,
                    case_number=case_number[:64],
                    kad_url=kad_url[:500],
                    status=ArbitrCase.STATUS_MONITORING,
                )
                stats["created"] += 1
                continue

            # Обновление существующего
            fields = []
            if (not ac.case_number or force) and case_number and ac.case_number != case_number:
                ac.case_number = case_number[:64]
                fields.append("case_number")
            if (not ac.kad_url or force) and kad_url and ac.kad_url != kad_url:
                ac.kad_url = kad_url[:500]
                fields.append("kad_url")
            # Если случайно был в SEARCHING а у нас уже есть данные —
            # переключаем в monitoring.
            if (case_number or kad_url) and ac.status == ArbitrCase.STATUS_SEARCHING:
                ac.status = ArbitrCase.STATUS_MONITORING
                fields.append("status")
            if fields:
                if not dry:
                    ac.save(update_fields=fields)
                stats["updated"] += 1
            else:
                stats["already_ok"] += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== ИТОГ ==="))
        for k in ("created", "updated", "would_create", "already_ok", "no_service"):
            self.stdout.write(f"  {k}: {stats[k]}")
