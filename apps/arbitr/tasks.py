"""Celery-таски мониторинга арбитражных дел.

Все таски этого модуля идут в очередь `arbitr` (см. CELERY_TASK_ROUTES) —
её обслуживает контейнер arbitr-runner с Playwright + Chromium.
"""
from __future__ import annotations

import logging
import time
from datetime import time as dtime

from celery import shared_task
from django.utils import timezone

from .models import ArbitrCase, ArbitrCheckLog
from .notifications import send_captcha_alert
from .parsers.kad import (
    KadCaptchaRequired,
    KadParserError,
    KadSession,
)

logger = logging.getLogger("arbitr")


# Окно работы парсера — после 18:00 и до 08:00 по локальному времени
# сервера (TZ Django — Europe/Moscow). Чтобы не нагружать сервер днём.
WORK_WINDOW_START = dtime(18, 0)
WORK_WINDOW_END = dtime(8, 0)


def _in_work_window() -> bool:
    now = timezone.localtime().time()
    # Окно пересекает полночь, поэтому условие OR.
    return now >= WORK_WINDOW_START or now < WORK_WINDOW_END


def _log_check(
    case: ArbitrCase, state: str, duration_ms: int = 0, notes: str = "",
) -> None:
    ArbitrCheckLog.objects.create(
        case=case, state=state, duration_ms=duration_ms, notes=notes,
    )


@shared_task(name="arbitr.kad_monitor_pending")
def kad_monitor_pending():
    """Ищет дело на kad для каждого ArbitrCase в статусе 'searching'.

    Один Playwright/Chromium на всю пачку — экономит cold-start и
    переиспользует cloudflare-куки.
    """
    if not _in_work_window():
        return {"skipped": "outside_work_window"}

    qs = ArbitrCase.objects.filter(
        status=ArbitrCase.STATUS_SEARCHING,
    ).select_related("service__client", "started_by__user")
    cases = list(qs)
    total = len(cases)
    logger.info("kad_monitor_pending: %d дел в поиске", total)

    if not cases:
        return {"pending_cases": 0}

    stats = {"ok": 0, "nothing": 0, "captcha": 0, "error": 0}
    try:
        with KadSession() as kad:
            for case in cases:
                stats[_search_one(kad, case)] += 1
    except KadCaptchaRequired as exc:
        # Капча на этапе захода на главную — пачка отменена, дальше нет смысла.
        logger.warning("kad_monitor_pending: captcha — aborting batch")
        for case in cases:
            _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
            send_captcha_alert(case, page_url=exc.page_url)
            stats["captcha"] += 1

    return {"pending_cases": total, **stats}


def _search_one(kad: KadSession, case: ArbitrCase) -> str:
    """Возвращает ключ для статистики: 'ok' | 'nothing' | 'captcha' | 'error'."""
    client = case.service.client
    fio = " ".join(filter(None, [
        client.last_name, client.first_name, client.patronymic,
    ])).strip()
    if not fio:
        _log_check(case, ArbitrCheckLog.STATE_ERROR, notes="у клиента не задано ФИО")
        return "error"

    started = time.monotonic()
    try:
        hits = kad.search_by_party(fio)
    except KadCaptchaRequired as exc:
        _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        send_captcha_alert(case, page_url=exc.page_url)
        return "captcha"
    except NotImplementedError:
        _log_check(
            case, ArbitrCheckLog.STATE_ERROR,
            notes="Парсер search_by_party ещё не реализован",
        )
        return "error"
    except (KadParserError, Exception) as exc:  # noqa: BLE001 — логируем всё
        logger.exception("kad: ошибка поиска для дела %s", case.id)
        _log_check(case, ArbitrCheckLog.STATE_ERROR, notes=str(exc)[:1000])
        case.last_error = str(exc)[:2000]
        case.last_check_at = timezone.now()
        case.last_check_ok = False
        case.save(update_fields=["last_error", "last_check_at", "last_check_ok"])
        return "error"

    duration_ms = int((time.monotonic() - started) * 1000)
    if not hits:
        _log_check(
            case, ArbitrCheckLog.STATE_NOTHING, duration_ms=duration_ms,
            notes=f"По ФИО '{fio}' дела не найдены",
        )
        case.last_check_at = timezone.now()
        case.last_check_ok = True
        case.save(update_fields=["last_check_at", "last_check_ok"])
        return "nothing"

    # Найдено хотя бы что-то — пока что не двигаем case в monitoring сами:
    # сотрудник должен подтвердить, что именно ЭТО дело принадлежит клиенту.
    # TODO: сохранить найденные KadSearchHit'ы куда-то для UI выбора.
    notes = "Найдено: " + "; ".join(
        f"{h.case_number} ({h.court_name})" for h in hits[:5]
    )
    _log_check(case, ArbitrCheckLog.STATE_OK, duration_ms=duration_ms, notes=notes)
    case.last_check_at = timezone.now()
    case.last_check_ok = True
    case.save(update_fields=["last_check_at", "last_check_ok"])
    return "ok"


@shared_task(name="arbitr.kad_monitor_case")
def kad_monitor_case():
    """Парсит карточку дела на kad для каждого ArbitrCase в 'monitoring'."""
    if not _in_work_window():
        return {"skipped": "outside_work_window"}

    qs = ArbitrCase.objects.filter(
        status=ArbitrCase.STATUS_MONITORING,
    ).select_related("service__client", "started_by__user")
    cases = list(qs)
    total = len(cases)
    logger.info("kad_monitor_case: %d дел в мониторинге", total)
    if not cases:
        return {"monitoring_cases": 0}

    stats = {"ok": 0, "nothing": 0, "captcha": 0, "error": 0}
    try:
        with KadSession() as kad:
            for case in cases:
                if not case.kad_url:
                    _log_check(case, ArbitrCheckLog.STATE_ERROR, notes="kad_url пуст")
                    stats["error"] += 1
                    continue
                started = time.monotonic()
                try:
                    info = kad.parse_case(case.kad_url)
                except KadCaptchaRequired as exc:
                    _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
                    send_captcha_alert(case, page_url=exc.page_url)
                    stats["captcha"] += 1
                    continue
                except NotImplementedError:
                    _log_check(
                        case, ArbitrCheckLog.STATE_ERROR,
                        notes="Парсер parse_case ещё не реализован",
                    )
                    stats["error"] += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.exception("kad: ошибка парсинга дела %s", case.id)
                    _log_check(case, ArbitrCheckLog.STATE_ERROR, notes=str(exc)[:1000])
                    stats["error"] += 1
                    continue

                duration_ms = int((time.monotonic() - started) * 1000)
                # TODO: смёрджить info.events в ArbitrEvent (idempotent по kad_event_id).
                _log_check(
                    case, ArbitrCheckLog.STATE_OK, duration_ms=duration_ms,
                    notes=f"events={len(info.events)}",
                )
                case.court_name = info.court_name or case.court_name
                case.judge = info.judge or case.judge
                if info.instances:
                    case.instances = info.instances
                case.last_check_at = timezone.now()
                case.last_check_ok = True
                case.save(update_fields=[
                    "court_name", "judge", "instances",
                    "last_check_at", "last_check_ok",
                ])
                stats["ok"] += 1
    except KadCaptchaRequired as exc:
        logger.warning("kad_monitor_case: captcha — aborting batch")
        for case in cases:
            _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
            send_captcha_alert(case, page_url=exc.page_url)
            stats["captcha"] += 1

    return {"monitoring_cases": total, **stats}
