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

  # батчи по 10 с паузами по 3 мин и первичной паузой 40 мин (gentle режим)
  python manage.py parse_all_monitoring --batch-size 10 --sleep-between 180 --initial-cooldown 2400
"""
import time
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from apps.arbitr import cooldown
from apps.arbitr.models import ArbitrCase
from apps.arbitr.parsers.kad import KadCaptchaRequired, KadSession
from apps.arbitr.tasks import _parse_one


def _fmt_eta(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч{m:02d}м"
    return f"{m}м{s:02d}с"


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
        parser.add_argument(
            "--batch-size", type=int, default=0,
            help="Размер батча в одной KadSession (0 = весь батч в одной сессии). "
                 "Между батчами — пауза --sleep-between и НОВАЯ KadSession "
                 "(свежий Chrome помогает после остыва kad-IP).",
        )
        parser.add_argument(
            "--sleep-between", type=int, default=0,
            help="Пауза между батчами в секундах (имеет смысл с --batch-size).",
        )
        parser.add_argument(
            "--initial-cooldown", type=int, default=0,
            help="Пауза ПЕРЕД первым батчем в секундах "
                 "(чтобы дать kad-IP остыть после предыдущей капчи).",
        )
        parser.add_argument(
            "--stop-on-captcha", action="store_true",
            help="Игнорируется — теперь любая капча активирует глобальный "
                 "12ч cooldown и прогон ВСЕГДА останавливается.",
        )
        parser.add_argument(
            "--ignore-cooldown", action="store_true",
            help="Запустить даже если активен глобальный cooldown после "
                 "недавней капчи (kad почти наверняка снова покажет капчу).",
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

        # Глобальный cooldown после недавней капчи от kad. Без флага
        # --ignore-cooldown — отказываемся стартовать, чтобы не нагенерить
        # пустых попыток + не флудить алёртами в MAX.
        if cooldown.is_active() and not opts["ignore_cooldown"]:
            until = cooldown.until()
            raise CommandError(
                f"Активен глобальный cooldown после капчи. Возобновится: "
                f"{until:%d.%m %H:%M} (МСК). Снять вручную: "
                f"python manage.py arbitr_clear_cooldown."
            )

        cases = list(qs)
        self.stdout.write(f"К прогону: {len(cases)} кейсов")
        if opts["dry_run"] or not cases:
            for c in cases[:10]:
                self.stdout.write(f"  {c.case_number:25s} {c.service.client}")
            if len(cases) > 10:
                self.stdout.write(f"  … и ещё {len(cases) - 10}")
            return

        batch_size = opts["batch_size"] or len(cases)
        sleep_between = opts["sleep_between"]
        initial_cooldown = opts["initial_cooldown"]

        # Разбиваем на батчи
        batches = [cases[i:i + batch_size] for i in range(0, len(cases), batch_size)]
        self.stdout.write(
            f"Батчей: {len(batches)} по ~{batch_size}; "
            f"пауза между {sleep_between}с; "
            f"первичная {initial_cooldown}с"
        )

        if initial_cooldown:
            self.stdout.write(f"⏳ Первичная пауза {_fmt_eta(initial_cooldown)}…")
            time.sleep(initial_cooldown)

        stats = Counter()
        started = time.monotonic()
        done = 0
        aborted = False
        try:
            for bi, batch in enumerate(batches, 1):
                if aborted:
                    stats["captcha"] += len(batch)
                    continue
                self.stdout.write(self.style.NOTICE(
                    f"\n── Батч {bi}/{len(batches)} ({len(batch)} кейсов) ──"
                ))
                with KadSession() as kad:
                    for case in batch:
                        done += 1
                        try:
                            result = _parse_one(kad, case)
                        except KadCaptchaRequired as exc:
                            self.stdout.write(self.style.ERROR(
                                f"[{done}/{len(cases)}] CAPTCHA — глобальный cooldown 12ч активирован ({exc.page_url})"
                            ))
                            stats["captcha"] += 1
                            aborted = True
                            remaining_in_batch = len(batch) - (done - (bi-1)*batch_size)
                            stats["captcha"] += remaining_in_batch
                            done += remaining_in_batch
                            break
                        stats[result] += 1
                        if result == "captcha":
                            # _parse_one уже активировал cooldown внутри,
                            # дальше дёргать kad бессмысленно.
                            self.stdout.write(self.style.ERROR(
                                f"[{done}/{len(cases)}] {case.case_number}: captcha (cooldown активен) — стоп"
                            ))
                            aborted = True
                            remaining_in_batch = len(batch) - (done - (bi-1)*batch_size)
                            stats["captcha"] += remaining_in_batch
                            done += remaining_in_batch
                            break
                        el = int(time.monotonic() - started)
                        avg = el / done
                        eta = int(avg * (len(cases) - done) + sleep_between * (len(batches) - bi))
                        self.stdout.write(
                            f"[{done}/{len(cases)}] {case.case_number:25s} "
                            f"→ {result:8s}  ср.{avg:.0f}с  ост.~{_fmt_eta(eta)}"
                        )
                if bi < len(batches) and sleep_between and not aborted:
                    self.stdout.write(f"⏳ Пауза {_fmt_eta(sleep_between)} перед следующим батчем…")
                    time.sleep(sleep_between)
        finally:
            total = int(time.monotonic() - started)
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(
                f"=== ИТОГ ({_fmt_eta(total)}) ==="
            ))
            for k in ("ok", "nothing", "error", "captcha"):
                self.stdout.write(f"  {k}: {stats[k]}")
