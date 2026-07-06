"""Массовая авто-пауза «мёртвых» дел: MONITORING с самым свежим событием
старше N дней → PAUSED. Юрист руками может вернуть в MONITORING.

Основной механизм — прямо в _parse_one (после каждого парсинга проверяем и
переводим). Эта команда — для однократного backfill'а после включения
логики: пробегает по всем MONITORING-кейсам, ставит паузы БЕЗ вызова
парсера (не идём на kad).

  python manage.py arbitr_auto_pause_stale --dry-run
  python manage.py arbitr_auto_pause_stale
  python manage.py arbitr_auto_pause_stale --days 730   # свой порог
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Max
from django.utils import timezone

from apps.arbitr.models import ArbitrCase, ArbitrCheckLog
from apps.arbitr.tasks import AUTO_PAUSE_STALE_DAYS


class Command(BaseCommand):
    help = "Перевести в PAUSED все MONITORING-дела с самой свежей записью старше N дней."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--days", type=int, default=AUTO_PAUSE_STALE_DAYS,
            help=f"Порог протухания в днях (деф: {AUTO_PAUSE_STALE_DAYS}).",
        )

    def handle(self, *args, **opts):
        days = opts["days"]
        dry = opts["dry_run"]
        threshold = timezone.now().date() - timedelta(days=days)

        qs = (
            ArbitrCase.objects
            .filter(status=ArbitrCase.STATUS_MONITORING)
            .annotate(max_ev=Max("events__event_date"))
            .filter(max_ev__lt=threshold)
            .select_related("service__client")
            .order_by("max_ev")
        )
        total_mon = ArbitrCase.objects.filter(status=ArbitrCase.STATUS_MONITORING).count()
        to_pause = qs.count()
        self.stdout.write(
            f"MONITORING всего: {total_mon}, порог: событие < {threshold:%d.%m.%Y}, "
            f"кандидатов на паузу: {to_pause}"
        )
        if to_pause == 0:
            return

        # Показать первые 5
        for c in qs[:5]:
            self.stdout.write(
                f"  {c.case_number:20s} last_event={c.max_ev:%d.%m.%Y}  "
                f"client={c.service.client}"
            )
        if to_pause > 5:
            self.stdout.write(f"  … и ещё {to_pause - 5}")

        if dry:
            self.stdout.write(self.style.WARNING("--dry-run — ничего не меняем."))
            return

        # Пакетно ставим PAUSED (case.save + один лог на каждое дело).
        updated = 0
        for c in qs.iterator(chunk_size=200):
            c.status = ArbitrCase.STATUS_PAUSED
            c.save(update_fields=["status"])
            ArbitrCheckLog.objects.create(
                case=c, state=ArbitrCheckLog.STATE_OK,
                notes=(
                    f"Авто-пауза (backfill): последнее событие "
                    f"{c.max_ev:%d.%m.%Y}, нет активности > {days} дней"
                ),
            )
            updated += 1
            if updated % 100 == 0:
                self.stdout.write(f"  … {updated}/{to_pause}")

        self.stdout.write(self.style.SUCCESS(f"Готово: PAUSED поставлено {updated}"))
