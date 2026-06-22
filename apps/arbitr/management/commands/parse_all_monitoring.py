"""Прогнать парсер по всем MONITORING-кейсам (один Chrome на весь батч).

Зачем: разовая массовая операция, когда нужно срочно «протрясти» большую
пачку дел не дожидаясь следующего автотаска `arbitr.kad_monitor_case`.
По умолчанию — только те, что ни разу не парсились (`last_check_at=NULL`),
и только в группе БФЛ «Реструктуризация»/«Реализация».

  # dry-run: только посчитать
  python manage.py parse_all_monitoring --dry-run

  # боевой прогон только новых (unparsed) кейсов
  python manage.py parse_all_monitoring

  # тест на первых 5
  python manage.py parse_all_monitoring --limit 5

  # все MONITORING (включая ранее парсенные) — для full re-scan
  python manage.py parse_all_monitoring --all
"""
import time
from collections import Counter

from django.core.management.base import BaseCommand

from apps.arbitr.models import ArbitrCase
from apps.arbitr.parsers.kad import KadCaptchaRequired, KadSession
from apps.arbitr.tasks import _parse_one


class Command(BaseCommand):
    help = "Массовый прогон парсера по MONITORING-кейсам (один Chrome на батч)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--all", action="store_true",
            help="Без фильтра last_check_at — прогонять ВСЕ MONITORING-кейсы, "
                 "включая ранее парсенные.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Ограничить количество (0 = без лимита).",
        )
        parser.add_argument(
            "--service-statuses", default="Реструктуризация,Реализация",
            help="Названия common_status через запятую "
                 "(деф: Реструктуризация,Реализация). "
                 "'*' = все статусы.",
        )

    def handle(self, *args, **opts):
        qs = ArbitrCase.objects.filter(
            status=ArbitrCase.STATUS_MONITORING,
            service__name__short_name__icontains="БФЛ",
        ).select_related(
            "service__client", "service__region", "started_by__user",
        )

        statuses = opts["service_statuses"].strip()
        if statuses != "*":
            qs = qs.filter(
                service__common_status__name__in=[
                    s.strip() for s in statuses.split(",") if s.strip()
                ],
            )
        if not opts["all"]:
            qs = qs.filter(last_check_at__isnull=True)
        qs = qs.order_by("created_at")
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        cases = list(qs)
        self.stdout.write(f"К прогону: {len(cases)} кейсов")
        if opts["dry_run"] or not cases:
            for c in cases[:10]:
                self.stdout.write(f"  {c.case_number:25s} {c.service.client}")
            if len(cases) > 10:
                self.stdout.write(f"  … и ещё {len(cases) - 10}")
            return

        stats = Counter()
        started = time.monotonic()
        try:
            with KadSession() as kad:
                for i, case in enumerate(cases, 1):
                    try:
                        result = _parse_one(kad, case)
                    except KadCaptchaRequired as exc:
                        self.stdout.write(self.style.WARNING(
                            f"[{i}/{len(cases)}] CAPTCHA — abort batch ({exc.page_url})"
                        ))
                        stats["captcha"] += len(cases) - i + 1
                        break
                    stats[result] += 1
                    el = int(time.monotonic() - started)
                    self.stdout.write(
                        f"[{i}/{len(cases)}] {case.case_number:25s} "
                        f"→ {result:8s}  (всего {el}с, в среднем {el/i:.1f}с)"
                    )
        finally:
            total = int(time.monotonic() - started)
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(
                f"=== ИТОГ ({total}с = {total//60}м{total%60}с) ==="
            ))
            for k in ("ok", "nothing", "error", "captcha"):
                self.stdout.write(f"  {k}: {stats[k]}")
