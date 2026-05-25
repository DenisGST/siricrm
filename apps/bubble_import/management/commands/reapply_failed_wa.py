"""Перепрогнать MessageWSP-записи, упавшие с «Клиент с номером … не найден».

Используется после `sync_projectbfl_aliases` — теперь у клиентов появились
дополнительные WhatsApp-номера через ClientPhone, и applier найдёт их.

Стратегия: помечаем подходящие записи `approved=True, status='pending'`,
обнуляем `error/imported_at`, и одной пачкой прогоняем через apply_record.
"""
import logging
import time

from django.core.management.base import BaseCommand

from apps.bubble_import.appliers import apply_record
from apps.bubble_import.models import BubbleRecord

logger = logging.getLogger("bubble_import")


class Command(BaseCommand):
    help = "Перепрогнать MessageWSP с ошибкой «клиент не найден»"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Сколько записей обработать (0 = все)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Только посчитать, не применять",
        )

    def handle(self, *args, **opts):
        qs = BubbleRecord.objects.filter(
            entity="MessageWSP",
            status="error",
            error__icontains="Клиент с номером",
        )
        total = qs.count()
        self.stdout.write(f"Найдено {total} ошибочных записей MessageWSP")
        if opts["dry_run"]:
            return
        limit = opts["limit"] or total

        # Сначала помечаем approved+pending, чтобы apply_record не пропустил.
        BubbleRecord.objects.filter(pk__in=qs.values("pk")[:limit]).update(
            approved=True, status="pending", error="", imported_at=None,
        )
        ids = list(BubbleRecord.objects.filter(
            entity="MessageWSP", status="pending", approved=True,
        ).values_list("pk", flat=True)[:limit])

        n = 0
        ok = err = sk = 0
        t0 = time.time()
        for pk in ids:
            rec = BubbleRecord.objects.filter(pk=pk).first()
            if rec is None:
                continue
            status = apply_record(rec)
            n += 1
            if status == "imported":
                ok += 1
            elif status == "error":
                err += 1
            else:
                sk += 1
            if n % 200 == 0:
                self.stdout.write(
                    f"  {n}/{limit} — ok {ok}, err {err}, sk {sk}, "
                    f"{n/(time.time()-t0):.1f}/сек"
                )
        self.stdout.write(self.style.SUCCESS(
            f"Готово: {n} обработано, импорт {ok}, ошибки {err}, пропуск {sk}"
        ))
