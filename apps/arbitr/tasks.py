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

from . import cooldown
from .models import ArbitrAttachment, ArbitrCase, ArbitrCheckLog, ArbitrEvent
from .notifications import handle_captcha
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

    Один Chrome-инстанс на всю пачку — экономит cold-start и
    переиспользует кадровые cookies. Окно работы НЕ применяем:
    поиск по ФИО — лёгкая операция (~12с/дело), может работать
    круглосуточно. Расписание задаётся в django-celery-beat
    (`arbitr-kad-monitor-pending`).
    """
    if cooldown.is_active():
        logger.info("kad_monitor_pending: cooldown active until %s — пропускаем", cooldown.until())
        return {"skipped": "captcha_cooldown", "until": cooldown.until().isoformat()}

    qs = ArbitrCase.objects.filter(
        status=ArbitrCase.STATUS_SEARCHING,
    ).select_related("service__client", "service__region", "started_by__user")
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
                if cooldown.is_active():
                    # Внутри _search_one схватили капчу → активировался cooldown
                    # → остальные кейсы этой пачки пропускаем, не дёргая kad.
                    break
    except KadCaptchaRequired as exc:
        # Капча на этапе захода на главную — активируем cooldown, одиночный
        # алёрт. Остаток пачки НЕ дёргаем (раньше тут был флуд из N алёртов).
        logger.warning("kad_monitor_pending: captcha — aborting batch")
        _log_check(cases[0], ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        handle_captcha(cases[0], page_url=exc.page_url)
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

    # Фильтр по региону — без него по «Иванову И. И.» kad может вернуть
    # десятки дел из разных судов РФ. Префикс case_number у kad — «АNN»
    # где NN = Region.number (12=Волгоград, 40=Москва, 41=МО, 56=СПб …).
    # search_by_party потом отфильтрует по case_number.upper().startswith(prefix).
    region = case.service.region if case.service else None
    court_code = f"А{region.number}" if region and region.number else ""

    started = time.monotonic()
    try:
        hits = kad.search_by_party(fio, court_code=court_code)
    except KadCaptchaRequired as exc:
        _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        handle_captcha(case, page_url=exc.page_url)
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

    # Найдено хотя бы что-то — сохраняем в case.search_hits для UI выбора.
    # Сотрудник в карточке дела увидит список кандидатов и нажмёт «Это моё дело»
    # → confirm_hit-view выставит case_number/kad_url и переведёт в MONITORING.
    hits_payload = [
        {
            "case_number": h.case_number,
            "kad_url": h.kad_url,
            "court_name": h.court_name,
            "parties": list(h.parties),
            "filed_at": h.filed_at,
        }
        for h in hits
    ]
    case.search_hits = hits_payload
    case.search_hits_at = timezone.now()
    case.last_check_at = timezone.now()
    case.last_check_ok = True
    case.save(update_fields=[
        "search_hits", "search_hits_at", "last_check_at", "last_check_ok",
    ])

    notes = f"Найдено {len(hits_payload)} кандидат(ов): " + "; ".join(
        f"{h.case_number} ({h.court_name})" for h in hits[:5]
    )
    _log_check(case, ArbitrCheckLog.STATE_OK, duration_ms=duration_ms, notes=notes)
    return "ok"


@shared_task(name="arbitr.kad_monitor_case")
def kad_monitor_case():
    """Парсит карточку дела на kad для каждого ArbitrCase в 'monitoring'."""
    if not _in_work_window():
        return {"skipped": "outside_work_window"}
    if cooldown.is_active():
        logger.info("kad_monitor_case: cooldown active until %s — пропускаем", cooldown.until())
        return {"skipped": "captcha_cooldown", "until": cooldown.until().isoformat()}

    qs = ArbitrCase.objects.filter(
        status=ArbitrCase.STATUS_MONITORING,
    ).select_related("service__client", "service__region", "started_by__user")
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
                if cooldown.is_active():
                    # _parse_one внутри схватил капчу → cooldown активен →
                    # остаток пачки пропускаем (бессмысленно дёргать kad).
                    break
    except KadCaptchaRequired as exc:
        logger.warning("kad_monitor_case: captcha — aborting batch")
        _log_check(cases[0], ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        handle_captcha(cases[0], page_url=exc.page_url)
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
        handle_captcha(case, page_url=exc.page_url)
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

    # Снимок cookies main-сессии — нужны download-сессии чтобы kad её
    # «доверял» без повторного поиска (а search в download_mode сломан
    # из-за PDF prefs).
    try:
        source_cookies = kad.driver.get_cookies()
    except Exception:
        source_cookies = []

    # Скачиваем PDF'ы новых вложений в S3 (best-effort: ошибка отдельного
    # документа не валит обработку дела целиком). ОТДЕЛЬНАЯ download-сессия
    # — её Chrome имеет PDF prefs, которые ломают search-flow в main.
    downloaded = _download_new_attachments(case, source_cookies=source_cookies)

    _log_check(
        case, ArbitrCheckLog.STATE_OK, duration_ms=duration_ms,
        notes=(
            f"events={len(info.events)} "
            f"new={persisted['new_events']} "
            f"docs+={persisted['new_attachments']} "
            f"dl={downloaded['ok']}/{downloaded['ok'] + downloaded['failed']}"
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


def _download_new_attachments(
    case: ArbitrCase, *, source_cookies: list,
) -> dict:
    """Качает все ArbitrAttachment этого дела без stored_file → S3.

    Открывает ОТДЕЛЬНУЮ download-сессию (Chrome с PDF prefs) — этой сессии
    нельзя делать search/parse (PDF prefs ломают anti-bot), но качать PDF
    можно. Cookies от main-сессии прокидываем чтобы kad доверял нам без
    повторного UI-поиска.

    Best-effort: ошибки скачивания отдельного файла логируем и идём дальше.
    Captcha — поднимаем выше (KadCaptchaRequired), пусть batch остановится.
    Возвращает {'ok': N, 'failed': M, 'locked': K, 'skipped': X}.
    """
    from apps.files.models import StoredFile  # лениво — кросс-аппный импорт
    from apps.files.s3_utils import upload_file_to_s3

    stats = {"ok": 0, "failed": 0, "locked": 0, "skipped": 0}
    qs_list = list(
        ArbitrAttachment.objects.filter(
            event__case=case, stored_file__isnull=True,
        ).select_related("event")
    )
    if not qs_list:
        return stats

    # Открываем ОТДЕЛЬНУЮ Chrome-сессию с PDF prefs.
    with KadSession(download_mode=True) as dl:
        if source_cookies:
            dl.load_kad_cookies(source_cookies)
        # Активируем kad-trust открытием карточки. В download_mode warmup-
        # поиск сломан (PDF prefs детектятся anti-bot'ом), но с cookies
        # main-сессии прямой GET карточки работает.
        dl.driver.get(case.kad_url)
        time.sleep(3)
        try:
            dl._raise_if_captcha()  # noqa: SLF001
        except KadCaptchaRequired:
            handle_captcha(case, page_url=case.kad_url)
            raise

        for att in qs_list:
            if att.is_locked:
                stats["locked"] += 1
                continue
            if not att.kad_url:
                stats["skipped"] += 1
                continue
            try:
                content, content_type = dl.download_pdf(
                    att.kad_url, referer=case.kad_url,
                )
            except KadCaptchaRequired:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "kad: PDF не скачался (att=%s url=%s): %s",
                    att.id, att.kad_url, exc,
                )
                stats["failed"] += 1
                continue

            # Имя для S3 + StoredFile. Если kad name пустой — используем att.id.
            ext = "pdf"
            if "pdf" not in content_type.lower():
                if ".pdf" in att.kad_url.lower():
                    ext = "pdf"
                else:
                    ext = "bin"
            safe_name = (att.name or f"document-{att.id}").strip()
            if not safe_name.lower().endswith(f".{ext}"):
                safe_name = f"{safe_name}.{ext}"
            # StoredFile.filename = CharField(max_length=255). У kad заголовки
            # документов бывают по 300+ символов («[Подписано] Отложить
            # судебное разбирательство (ст.157, 158, 225_15 АПК)»+.pdf).
            if len(safe_name) > 250:
                head = safe_name[: 250 - len(ext) - 4]
                safe_name = f"{head}….{ext}"

            try:
                bucket, key = upload_file_to_s3(
                    content,
                    prefix=f"arbitr/{case.id}",
                    filename=safe_name,
                    content_type=content_type,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "kad: S3 upload failed for att %s: %s", att.id, exc,
                )
                stats["failed"] += 1
                continue

            stored = StoredFile.objects.create(
                bucket=bucket, key=key, filename=safe_name,
                content_type=content_type, size=len(content),
            )
            att.stored_file = stored
            att.save(update_fields=["stored_file"])
            stats["ok"] += 1

    return stats


@shared_task(name="arbitr.kad_throttled_one")
def kad_throttled_one():
    """Парсит ОДНО дело из MONITORING — самое «протухшее» (минимальный
    last_check_at, NULL первыми).

    Идея: вместо больших батчей по 200 кейсов раз в 6ч (которые гарантированно
    нарывались на капчу), beat дёргает эту таску каждые 5 минут → 1 кейс / 5 мин
    = 288 кейсов/сутки. kad-IP не успевает «прогореть», капча почти не
    срабатывает. На dev сейчас 1192 кейса → полный круг ~4 суток, для рабочей
    нагрузки 250 active+500 stale = ~2.6 суток.

    Если активен 12ч captcha-cooldown — таска тихо выходит.
    """
    if cooldown.is_active():
        return {"skipped": "captcha_cooldown", "until": cooldown.until().isoformat()}

    from django.db.models import F
    case = (
        ArbitrCase.objects
        .filter(status=ArbitrCase.STATUS_MONITORING)
        .select_related("service__client", "service__region", "started_by__user")
        .order_by(F("last_check_at").asc(nulls_first=True))
        .first()
    )
    if case is None:
        return {"skipped": "no_cases"}

    logger.info(
        "kad_throttled_one: case=%s number=%s prev_check=%s",
        case.id, case.case_number, case.last_check_at,
    )

    try:
        with KadSession() as kad:
            result = _parse_one(kad, case)
    except KadCaptchaRequired as exc:
        _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        handle_captcha(case, page_url=exc.page_url)
        result = "captcha"

    return {"case_id": str(case.id), "case_number": case.case_number, "result": result}


@shared_task(name="arbitr.kad_monitor_one_case")
def kad_monitor_one_case(case_id: str):
    """Ручной запуск парсинга ОДНОГО дела (минуя work-window).

    Из UI: кнопка «Парсить сейчас» в карточке дела. Не зависит от
    окна 18-08 — сотрудник видит результат сразу. В зависимости от
    status — search_by_party (SEARCHING) или parse_case (MONITORING).
    """
    try:
        case = ArbitrCase.objects.select_related(
            "service__client", "service__region", "started_by__user",
        ).get(pk=case_id)
    except ArbitrCase.DoesNotExist:
        logger.warning("kad_monitor_one_case: case %s не найден", case_id)
        return {"error": "case_not_found"}

    logger.info(
        "kad_monitor_one_case: case=%s status=%s (ручной запуск)",
        case.id, case.status,
    )

    if cooldown.is_active():
        logger.info("kad_monitor_one_case: cooldown until %s — отказ", cooldown.until())
        _log_check(
            case, ArbitrCheckLog.STATE_CAPTCHA,
            notes=f"Парсинг приостановлен (cooldown до {cooldown.until():%d.%m %H:%M} МСК)",
        )
        from django.core.cache import cache  # noqa: WPS433
        cache.delete(f"arbitr:active_task:{case.id}")
        return {"case_id": str(case.id), "result": "cooldown", "until": cooldown.until().isoformat()}

    try:
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
            handle_captcha(case, page_url=exc.page_url)
            result = "captcha"

        return {"case_id": str(case.id), "result": result}
    finally:
        # Сигнал «таск завершился» для UI-поллера в views.case_card_partial.
        # Тот же ключ что устанавливает views.case_run.
        from django.core.cache import cache  # noqa: WPS433 — local
        cache.delete(f"arbitr:active_task:{case.id}")
