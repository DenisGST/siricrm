"""Celery-таски мониторинга арбитражных дел.

Все таски этого модуля идут в очередь `arbitr` (см. CELERY_TASK_ROUTES) —
её обслуживает контейнер arbitr-runner с Selenium + Chrome + Xvfb.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, time as dtime

from celery import shared_task
from django.utils import timezone

from .models import ArbitrAttachment, ArbitrCase, ArbitrCheckLog, ArbitrEvent
from .notifications import send_captcha_alert
from .parsers.kad import (
    KadCaptchaRequired,
    KadCaseInfo,
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


def _parse_kad_date(s: str):
    """kad даёт даты как 'DD.MM.YYYY' — превращаем в date|None."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _persist_case_info(case: ArbitrCase, info: KadCaseInfo) -> dict:
    """Сохраняет ArbitrEvent/Attachment из result'а парсера.

    Идемпотентно: UNIQUE по (case, kad_event_id) в модели + ignore_conflicts
    на bulk_create. Возвращает {'new_events', 'new_attachments'}.
    """
    existing = set(case.events.values_list("kad_event_id", flat=True))
    new_events_data: list[tuple[ArbitrEvent, list[dict]]] = []
    for ev in info.events:
        if not ev.kad_event_id:
            continue  # без id не сможем держать идемпотентность
        if ev.kad_event_id in existing:
            continue
        new_events_data.append((
            ArbitrEvent(
                case=case,
                kad_event_id=ev.kad_event_id,
                event_date=_parse_kad_date(ev.event_date),
                kind=ev.kind[:128],
                title=(ev.title or "")[:500],
                description=ev.description or "",
                raw={
                    "instance_id": ev.instance_id,
                    "kad_event_id": ev.kad_event_id,
                },
            ),
            ev.attachments or [],
        ))

    if not new_events_data:
        return {"new_events": 0, "new_attachments": 0}

    # bulk_create + перечитать чтобы получить id
    ArbitrEvent.objects.bulk_create(
        [e for e, _ in new_events_data], ignore_conflicts=True,
    )
    # Перечитать только что созданные — по kad_event_id
    created_ids = [e.kad_event_id for e, _ in new_events_data]
    fresh = {
        e.kad_event_id: e
        for e in case.events.filter(kad_event_id__in=created_ids)
    }

    new_atts: list[ArbitrAttachment] = []
    for ev_obj, atts in new_events_data:
        db_event = fresh.get(ev_obj.kad_event_id)
        if db_event is None:
            continue
        for att in atts:
            kad_url = (att.get("kad_url") or "").strip()
            if not kad_url:
                continue
            new_atts.append(ArbitrAttachment(
                event=db_event,
                name=(att.get("name") or "")[:500],
                kad_url=kad_url,
                is_locked=bool(att.get("is_locked")),
            ))
    if new_atts:
        ArbitrAttachment.objects.bulk_create(new_atts)

    return {
        "new_events": len(new_events_data),
        "new_attachments": len(new_atts),
    }


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
                stats[_parse_one(kad, case)] += 1
    except KadCaptchaRequired as exc:
        logger.warning("kad_monitor_case: captcha — aborting batch")
        for case in cases:
            _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
            send_captcha_alert(case, page_url=exc.page_url)
            stats["captcha"] += 1

    return {"monitoring_cases": total, **stats}


def _parse_one(kad: KadSession, case: ArbitrCase) -> str:
    """Парсит карточку одного дела. Возвращает ключ для статистики."""
    if not case.kad_url:
        _log_check(case, ArbitrCheckLog.STATE_ERROR, notes="kad_url пуст")
        return "error"

    started = time.monotonic()
    try:
        info = kad.parse_case(case.kad_url)
    except KadCaptchaRequired as exc:
        _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        send_captcha_alert(case, page_url=exc.page_url)
        return "captcha"
    except NotImplementedError:
        _log_check(
            case, ArbitrCheckLog.STATE_ERROR,
            notes="Парсер parse_case ещё не реализован",
        )
        return "error"
    except Exception as exc:  # noqa: BLE001
        logger.exception("kad: ошибка парсинга дела %s", case.id)
        _log_check(case, ArbitrCheckLog.STATE_ERROR, notes=str(exc)[:1000])
        case.last_error = str(exc)[:2000]
        case.last_check_at = timezone.now()
        case.last_check_ok = False
        case.save(update_fields=["last_error", "last_check_at", "last_check_ok"])
        return "error"

    duration_ms = int((time.monotonic() - started) * 1000)
    persisted = _persist_case_info(case, info)
    _log_check(
        case, ArbitrCheckLog.STATE_OK, duration_ms=duration_ms,
        notes=(
            f"events={len(info.events)} "
            f"new={persisted['new_events']} "
            f"docs+={persisted['new_attachments']}"
        ),
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
    return "ok"


@shared_task(name="arbitr.kad_monitor_one_case")
def kad_monitor_one_case(case_id: str):
    """Ручной запуск парсинга ОДНОГО дела (минуя work-window).

    Из UI: кнопка «Парсить сейчас» в карточке дела. Не зависит от
    окна 18-08 — сотрудник видит результат сразу. В зависимости от
    status — search_by_party (SEARCHING) или parse_case (MONITORING).
    """
    try:
        case = ArbitrCase.objects.select_related(
            "service__client", "started_by__user",
        ).get(pk=case_id)
    except ArbitrCase.DoesNotExist:
        logger.warning("kad_monitor_one_case: case %s не найден", case_id)
        return {"error": "case_not_found"}

    logger.info(
        "kad_monitor_one_case: case=%s status=%s (ручной запуск)",
        case.id, case.status,
    )

    try:
        with KadSession() as kad:
            if case.status == ArbitrCase.STATUS_SEARCHING:
                result = _search_one(kad, case)
            elif case.status == ArbitrCase.STATUS_MONITORING:
                result = _parse_one(kad, case)
            else:
                _log_check(
                    case, ArbitrCheckLog.STATE_ERROR,
                    notes=f"Ручной запуск недоступен для статуса {case.status}",
                )
                return {"error": "wrong_status", "status": case.status}
    except KadCaptchaRequired as exc:
        _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        send_captcha_alert(case, page_url=exc.page_url)
        result = "captcha"

    return {"case_id": str(case.id), "result": result}
