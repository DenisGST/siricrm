"""Celery-таски мониторинга арбитражных дел. Сам парсер — заглушка
(будет реализован отдельно в apps/arbitr/parsers/kad.py после установки
Playwright в отдельном контейнере)."""
import logging
from datetime import time

from celery import shared_task
from django.utils import timezone

from .models import ArbitrCase, ArbitrCheckLog

logger = logging.getLogger("arbitr")


# Окно работы парсера — после 18:00 и до 08:00 по локальному времени
# сервера (TZ Django — Europe/Moscow). Чтобы не нагружать сервер днём.
WORK_WINDOW_START = time(18, 0)
WORK_WINDOW_END = time(8, 0)


def _in_work_window() -> bool:
    now = timezone.localtime().time()
    # Окно пересекает полночь, поэтому условие OR.
    return now >= WORK_WINDOW_START or now < WORK_WINDOW_END


@shared_task(name="arbitr.kad_monitor_pending")
def kad_monitor_pending():
    """Ищет дело на kad для каждого ArbitrCase в статусе 'searching'."""
    if not _in_work_window():
        return {"skipped": "outside_work_window"}
    qs = ArbitrCase.objects.filter(status=ArbitrCase.STATUS_SEARCHING)
    total = qs.count()
    logger.info("kad_monitor_pending: %d дел в поиске", total)
    # TODO(parser): apps.arbitr.parsers.kad.search_case(case)
    for case in qs:
        ArbitrCheckLog.objects.create(
            case=case, state=ArbitrCheckLog.STATE_ERROR,
            notes="Парсер ещё не реализован",
        )
    return {"pending_cases": total, "stub": True}


@shared_task(name="arbitr.kad_monitor_case")
def kad_monitor_case():
    """Парсит карточку дела на kad для каждого ArbitrCase в 'monitoring'.
    Заглушка."""
    if not _in_work_window():
        return {"skipped": "outside_work_window"}
    qs = ArbitrCase.objects.filter(status=ArbitrCase.STATUS_MONITORING)
    total = qs.count()
    logger.info("kad_monitor_case: %d дел в мониторинге", total)
    return {"monitoring_cases": total, "stub": True}
