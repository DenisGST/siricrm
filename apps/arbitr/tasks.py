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
    if cooldown.is_active(""):
        logger.info("kad_monitor_pending: cooldown active until %s — пропускаем", cooldown.until(""))
        return {"skipped": "captcha_cooldown", "until": cooldown.until("").isoformat()}

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
                if cooldown.is_active(""):
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


def _search_one(kad: KadSession, case: ArbitrCase, runner_ip: str = "") -> str:
    """Возвращает ключ для статистики: 'ok' | 'nothing' | 'captcha' | 'error'."""
    client = case.service.client
    fio = " ".join(filter(None, [
        client.last_name, client.first_name, client.patronymic,
    ])).strip()
    if not fio:
        _log_check(case, ArbitrCheckLog.STATE_ERROR, notes="у клиента не задано ФИО")
        from datetime import timedelta
        now = timezone.now()
        case.last_check_at = now
        case.last_check_ok = False
        case.next_search_at = now + timedelta(hours=24)
        case.save(update_fields=["last_check_at", "last_check_ok", "next_search_at"])
        return "error"

    # Фильтр по региону — без него по «Иванову И. И.» kad может вернуть
    # десятки дел из разных судов РФ. Берём Region.arbitr_code — это код
    # АС субъекта РФ на kad.arbitr.ru (например, «А12» для Волгограда,
    # «А40» для Москвы, «А56» для СПб). НЕ совпадает с Region.number
    # (код субъекта РФ), у ФАС РФ своя нумерация — см. миграцию
    # crm/0094_region_arbitr_code_data.
    # Если arbitr_code не задан (новый/редкий регион) — фильтр пропускаем,
    # вернётся всё что нашёл kad по ФИО.
    region = case.service.region if case.service else None
    court_code = (region.arbitr_code or "").strip() if region else ""

    started = time.monotonic()
    try:
        hits = kad.search_by_party(fio, court_code=court_code)
    except KadCaptchaRequired as exc:
        _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        handle_captcha(case, page_url=exc.page_url, ip=runner_ip)
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
        from datetime import timedelta
        now = timezone.now()
        case.last_check_at = now
        case.last_check_ok = False
        # error при поиске — пробуем через час (без этого NULL next_search_at
        # → тот же кейс снова первый в очереди, бесконечный цикл).
        case.next_search_at = now + timedelta(hours=1)
        case.save(update_fields=[
            "last_error", "last_check_at", "last_check_ok", "next_search_at",
        ])
        return "error"

    duration_ms = int((time.monotonic() - started) * 1000)
    from datetime import timedelta
    now = timezone.now()
    if not hits:
        _log_check(
            case, ArbitrCheckLog.STATE_NOTHING, duration_ms=duration_ms,
            notes=f"По ФИО '{fio}' дела не найдены",
        )
        case.last_check_at = now
        case.last_check_ok = True
        # miss — следующий поиск через 3ч
        case.next_search_at = now + timedelta(hours=3)
        case.save(update_fields=[
            "last_check_at", "last_check_ok", "next_search_at",
        ])
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
    case.search_hits_at = now
    case.last_check_at = now
    case.last_check_ok = True
    # hit — нашли кандидатов, следующий поиск через 24ч (если юзер ничего не выберет)
    case.next_search_at = now + timedelta(hours=24)
    case.save(update_fields=[
        "search_hits", "search_hits_at",
        "last_check_at", "last_check_ok", "next_search_at",
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
    if cooldown.is_active(""):
        logger.info("kad_monitor_case: cooldown active until %s — пропускаем", cooldown.until(""))
        return {"skipped": "captcha_cooldown", "until": cooldown.until("").isoformat()}

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
                stats[_parse_one(kad, case)["result"]] += 1
                if cooldown.is_active(""):
                    # _parse_one внутри схватил капчу → cooldown активен →
                    # остаток пачки пропускаем (бессмысленно дёргать kad).
                    break
    except KadCaptchaRequired as exc:
        logger.warning("kad_monitor_case: captcha — aborting batch")
        _log_check(cases[0], ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        handle_captcha(cases[0], page_url=exc.page_url)
        stats["captcha"] += 1

    return {"monitoring_cases": total, **stats}


def _parse_one(kad: KadSession, case: ArbitrCase, runner_ip: str = "") -> dict:
    """Парсит карточку одного дела. Возвращает dict:
      {result: 'ok'|'nothing'|'error'|'captcha',
       new_events: N, new_files: M, remaining_files: R, duration_sec: S}
    Файлы качаются порциями до 5 за прогон (limit=5); если осталось больше —
    `remaining_files > 0`, докачается в следующий парсинг (через 24ч).
    После успеха пишет next_parse_at = now() + 24ч.
    """
    base = {"result": "error", "new_events": 0, "new_files": 0,
            "remaining_files": 0, "duration_sec": 0}
    if not case.kad_url:
        _log_check(case, ArbitrCheckLog.STATE_ERROR, notes="kad_url пуст")
        # Без kad_url дело парсить нельзя — отложим на 24ч, чтоб не зацикливался.
        from datetime import timedelta
        now = timezone.now()
        case.last_check_at = now
        case.last_check_ok = False
        case.next_parse_at = now + timedelta(hours=24)
        case.save(update_fields=["last_check_at", "last_check_ok", "next_parse_at"])
        return base

    started = time.monotonic()
    try:
        info = kad.parse_case(case.kad_url)
    except KadCaptchaRequired as exc:
        _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
        handle_captcha(case, page_url=exc.page_url, ip=runner_ip)
        return {**base, "result": "captcha",
                "duration_sec": int(time.monotonic() - started)}
    except NotImplementedError:
        _log_check(
            case, ArbitrCheckLog.STATE_ERROR,
            notes="Парсер parse_case ещё не реализован",
        )
        from datetime import timedelta
        now = timezone.now()
        case.last_check_at = now
        case.last_check_ok = False
        case.next_parse_at = now + timedelta(hours=24)
        case.save(update_fields=["last_check_at", "last_check_ok", "next_parse_at"])
        return {**base, "duration_sec": int(time.monotonic() - started)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("kad: ошибка парсинга дела %s", case.id)
        _log_check(case, ArbitrCheckLog.STATE_ERROR, notes=str(exc)[:1000])
        case.last_error = str(exc)[:2000]
        now = timezone.now()
        case.last_check_at = now
        case.last_check_ok = False
        # error — не зацикливать кейс, попробуем через час (а не сразу
        # снова, как было раньше: NULL next_parse_at → кейс снова первый).
        from datetime import timedelta
        case.next_parse_at = now + timedelta(hours=1)
        case.save(update_fields=[
            "last_error", "last_check_at", "last_check_ok", "next_parse_at",
        ])
        return {**base, "duration_sec": int(time.monotonic() - started)}

    duration_ms = int((time.monotonic() - started) * 1000)
    persisted = _persist_case_info(case, info)

    # Снимок cookies main-сессии — нужны download-сессии чтобы kad её
    # «доверял» без повторного поиска (а search в download_mode сломан
    # из-за PDF prefs).
    try:
        source_cookies = kad.driver.get_cookies()
    except Exception:
        source_cookies = []

    # Скачиваем PDF'ы новых вложений в S3 порциями по 5 (best-effort:
    # ошибка отдельного документа не валит обработку дела целиком).
    # ОТДЕЛЬНАЯ download-сессия — её Chrome имеет PDF prefs, которые
    # ломают search-flow в main.
    downloaded = _download_new_attachments(
        case, source_cookies=source_cookies, limit=5, runner_ip=runner_ip,
    )

    _log_check(
        case, ArbitrCheckLog.STATE_OK, duration_ms=duration_ms,
        notes=(
            f"events={len(info.events)} "
            f"new={persisted['new_events']} "
            f"docs+={persisted['new_attachments']} "
            f"dl={downloaded['ok']}/{downloaded['ok'] + downloaded['failed']} "
            f"left={downloaded['remaining']}"
        ),
    )
    now = timezone.now()
    case.court_name = info.court_name or case.court_name
    case.judge = info.judge or case.judge
    if info.instances:
        case.instances = info.instances
    case.last_check_at = now
    case.last_check_ok = True
    # «не чаще 1 раз в сутки» на это дело
    from datetime import timedelta
    case.next_parse_at = now + timedelta(hours=24)
    case.save(update_fields=[
        "court_name", "judge", "instances",
        "last_check_at", "last_check_ok", "next_parse_at",
    ])
    return {
        "result": "ok",
        "new_events": persisted["new_events"],
        "new_files": downloaded["ok"],
        "remaining_files": downloaded["remaining"],
        "duration_sec": int(time.monotonic() - started),
    }


def _download_new_attachments(
    case: ArbitrCase, *, source_cookies: list, limit: int = 5, runner_ip: str = "",
) -> dict:
    """Качает ArbitrAttachment этого дела без stored_file → S3, не более `limit`
    за один раз (по умолчанию 5 — анти-капча: каждый PDF-download — это
    отдельный запрос на kad, после ~20 подряд kad показывает капчу).

    Открывает ОТДЕЛЬНУЮ download-сессию (Chrome с PDF prefs) — этой сессии
    нельзя делать search/parse (PDF prefs ломают anti-bot), но качать PDF
    можно. Cookies от main-сессии прокидываем чтобы kad доверял нам без
    повторного UI-поиска.

    Best-effort: ошибки скачивания отдельного файла логируем и идём дальше.
    Captcha — поднимаем выше (KadCaptchaRequired), пусть batch остановится.

    Возвращает {'ok': N, 'failed': M, 'locked': K, 'skipped': X, 'remaining': R}.
    remaining — сколько ещё незакачанных осталось ПОСЛЕ этого прогона.
    """
    from apps.files.models import StoredFile  # лениво — кросс-аппный импорт
    from apps.files.s3_utils import upload_file_to_s3

    stats = {"ok": 0, "failed": 0, "locked": 0, "skipped": 0, "remaining": 0}
    base_qs = ArbitrAttachment.objects.filter(
        event__case=case, stored_file__isnull=True,
    ).select_related("event")
    pending_total = base_qs.count()
    if not pending_total:
        return stats
    qs_list = list(base_qs[:limit]) if limit and limit > 0 else list(base_qs)
    stats["remaining"] = max(0, pending_total - len(qs_list))

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
            handle_captcha(case, page_url=case.kad_url, ip=runner_ip)
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


def _kad_smart_one(runner_id: str):
    """Универсальный body для kad_smart_one_<runner_id>. Per-runner state
    (lock/throttle/counter/current_case) — изолированный, чтобы 3 runner'а
    парсили параллельно через разные outbound IP (см. ops/arbitr-snat-rotate.sh).

    Алгоритм каждого тика (beat 1× в 10с по своей очереди arbitr_<id>):
      0. captcha_cooldown (12ч глобально) → выйти молча.
      1. runner_disabled или runner_ip пуст (rotator не назначил IP в
         этом часу) → выйти.
      2. Per-runner throttle → выйти.
      3. Per-runner lock — выйти если занят (уже идёт парсинг).
      4. Атомарно (SELECT … FOR UPDATE SKIP LOCKED) забрать кейс и сразу
         поставить next_*_at = now+30мин («резерв», чтоб другие runners
         не взяли тот же). После парсинга _parse_one/_search_one ставят
         финальные next_*_at (24ч / 3ч / 1ч / 24ч).
      5. Парсим.
      6. Per-runner throttle по результату (3-15мин / 10с / 30мин / 60с).
      7. На success — алёрт в MAX. На captcha — global cooldown.
    """
    import random  # noqa: WPS433
    from datetime import timedelta
    from django.core.cache import cache
    from django.db import transaction
    from django.db.models import F, Q

    # Если rotator не назначил этому runner'у IP в этом часу (мало активных IP) —
    # пропускаем тик. Ключ `arbitr:runner_ip:<id>` пишет ops/arbitr-snat-rotate.sh
    # НАПРЯМУЮ в Redis (без Django-префикса :1:), поэтому читаем через redis-py.
    runner_ip = ""
    try:
        import redis as _redis  # noqa: WPS433
        from django.conf import settings as _settings  # noqa: WPS433
        _r = _redis.Redis.from_url(_settings.REDIS_URL)
        _v = _r.get(f"arbitr:runner_ip:{runner_id}")
        runner_ip = _v.decode("utf-8") if _v else ""
    except Exception:
        runner_ip = ""
    if not runner_ip:
        return {"skipped": "runner_disabled", "runner": runner_id}

    # Per-IP captcha cooldown — если этот IP отдыхает 12ч после капчи,
    # пропускаем тик. Другие runner'ы (на других IP) при этом могут
    # продолжать парсинг.
    if cooldown.is_active(runner_ip):
        return {"skipped": "captcha_cooldown", "ip": runner_ip}

    THROTTLE_KEY = f"arbitr:smart_throttle_until:{runner_id}"
    LOCK_KEY = f"arbitr:smart_lock:{runner_id}"
    COUNTER_KEY = f"arbitr:smart_parse_count:{runner_id}"
    CURRENT_KEY = f"arbitr:smart_current_case:{runner_id}"
    BREAK_EVERY = 8       # каждые N успешных парсингов
    BREAK_SECONDS = 1800  # пауза 30 мин

    def _set_throttle(seconds):
        # Храним ISO-таймстемп конца паузы — UI считывает и показывает ETA.
        end = (timezone.now() + timedelta(seconds=seconds)).isoformat()
        cache.set(THROTTLE_KEY, end, timeout=seconds)

    if cache.get(THROTTLE_KEY):
        return {"skipped": "throttle"}
    if not cache.add(LOCK_KEY, "1", timeout=600):
        return {"skipped": "lock_busy"}

    try:
        now = timezone.now()
        # Атомарный «забор» кейса: SELECT FOR UPDATE SKIP LOCKED + сразу
        # сдвигаем next_*_at в будущее (30 мин «резерв»), чтобы остальные
        # 2 runner'а не взяли тот же кейс. _parse_one/_search_one потом
        # выставят финальное next_*_at.
        RESERVE = timedelta(minutes=30)
        with transaction.atomic():
            candidate = (
                ArbitrCase.objects
                .select_for_update(skip_locked=True)
                .filter(status=ArbitrCase.STATUS_SEARCHING)
                .filter(Q(next_search_at__isnull=True) | Q(next_search_at__lte=now))
                .order_by(F("next_search_at").asc(nulls_first=True))
                .first()
            )
            kind = "search"
            if candidate is None:
                candidate = (
                    ArbitrCase.objects
                    .select_for_update(skip_locked=True)
                    .filter(status=ArbitrCase.STATUS_MONITORING)
                    .filter(Q(next_parse_at__isnull=True) | Q(next_parse_at__lte=now))
                    .order_by(F("next_parse_at").asc(nulls_first=True))
                    .first()
                )
                kind = "parse" if candidate else None
            if candidate is None:
                _set_throttle(60)
                return {"skipped": "nothing_ready", "runner": runner_id}
            # Резервируем
            if kind == "search":
                candidate.next_search_at = now + RESERVE
                candidate.save(update_fields=["next_search_at"])
            else:
                candidate.next_parse_at = now + RESERVE
                candidate.save(update_fields=["next_parse_at"])

        # Подгрузим related-поля отдельным запросом (внутри for-update нельзя).
        case = (
            ArbitrCase.objects
            .select_related("service__client", "service__region", "started_by__user")
            .get(pk=candidate.pk)
        )
        logger.info(
            "kad_smart_one[%s]: case=%s number=%s kind=%s ip=%s",
            runner_id, case.id, case.case_number, kind, runner_ip,
        )
        cache.set(CURRENT_KEY, str(case.id), timeout=600)

        try:
            with KadSession() as kad:
                if kind == "search":
                    sr = _search_one(kad, case, runner_ip=runner_ip)
                    # _search_one уже ставит next_search_at (3ч miss / 24ч hit)
                    _set_throttle(60)
                    return {"case_id": str(case.id), "kind": "search", "result": sr}
                # MONITORING
                pr = _parse_one(kad, case, runner_ip=runner_ip)
                if pr["result"] == "ok":
                    something_new = pr["new_events"] > 0 or pr["new_files"] > 0
                    # Считаем успешные парсинги — каждые BREAK_EVERY пауза 30 мин.
                    count = int(cache.get(COUNTER_KEY) or 0) + 1
                    if count >= BREAK_EVERY:
                        _set_throttle(BREAK_SECONDS)
                        cache.set(COUNTER_KEY, 0, timeout=86400)
                        pr["long_break"] = BREAK_SECONDS
                    else:
                        cache.set(COUNTER_KEY, count, timeout=86400)
                        # Случайная пауза 3-15 мин если что-то новое, иначе 10с.
                        # Источник kad-трафика — iptables SNAT, который host-side
                        # таймер `arbitr-snat-rotate.timer` ротирует раз в минуту
                        # среди активных IP по расписанию (см. скрипт). За время
                        # 3-15 мин паузы IP скорее всего успеет смениться.
                        delay = random.randint(180, 900) if something_new else 10
                        _set_throttle(delay)
                        pr["delay"] = delay
                    pr["parse_count"] = count
                    # короткое уведомление в MAX
                    from .notifications import send_parsed_alert
                    send_parsed_alert(
                        case,
                        new_events=pr["new_events"],
                        new_files=pr["new_files"],
                        duration_sec=pr["duration_sec"],
                    )
                elif pr["result"] == "captcha":
                    pass  # handle_captcha уже включил 12ч cooldown
                else:
                    _set_throttle(60)
                return {"case_id": str(case.id), "kind": "parse", **pr}
        except KadCaptchaRequired as exc:
            _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
            handle_captcha(case, page_url=exc.page_url, ip=runner_ip)
            return {"case_id": str(case.id), "result": "captcha"}
        except Exception as exc:  # noqa: BLE001
            # WebDriverException, OOM, что угодно — НЕ даём задаче упасть
            # с raised (иначе Celery бэкграундит retry, а главное — кейс
            # без next_*_at снова первый в очереди и зацикливается).
            logger.exception("kad_smart_one: ошибка на кейсе %s", case.id)
            _log_check(case, ArbitrCheckLog.STATE_ERROR, notes=str(exc)[:1000])
            from datetime import timedelta
            now = timezone.now()
            case.last_error = str(exc)[:2000]
            case.last_check_at = now
            case.last_check_ok = False
            if kind == "search":
                case.next_search_at = now + timedelta(hours=1)
                case.save(update_fields=[
                    "last_error", "last_check_at", "last_check_ok", "next_search_at",
                ])
            else:
                case.next_parse_at = now + timedelta(hours=1)
                case.save(update_fields=[
                    "last_error", "last_check_at", "last_check_ok", "next_parse_at",
                ])
            # Дать времени на восстановление (chromedriver, FD-лимиты)
            _set_throttle(120)
            return {"case_id": str(case.id), "result": "error", "exc": str(exc)[:200]}
    finally:
        cache.delete(LOCK_KEY)
        cache.delete(CURRENT_KEY)


# Три отдельных task-имени для celery routing'а — каждый в свою очередь
# arbitr_<a/b/c>. В docker-compose runner-a/b/c слушают свои очереди и идут
# к kad с разных outbound IP (host-side iptables SNAT по source-IP контейнера).
@shared_task(name="arbitr.kad_smart_one_a")
def kad_smart_one_a():
    return _kad_smart_one("a")


@shared_task(name="arbitr.kad_smart_one_b")
def kad_smart_one_b():
    return _kad_smart_one("b")


@shared_task(name="arbitr.kad_smart_one_c")
def kad_smart_one_c():
    return _kad_smart_one("c")


@shared_task(name="arbitr.kad_smart_one_d")
def kad_smart_one_d():
    return _kad_smart_one("d")


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

    # Какой IP сейчас в SNAT — для cooldown-проверки и алёрта при капче.
    # rotator пишет ключ напрямую (без django :1: префикса).
    current_ip = ""
    try:
        import redis as _redis  # noqa: WPS433
        from django.conf import settings as _settings  # noqa: WPS433
        _r = _redis.Redis.from_url(_settings.REDIS_URL)
        _v = _r.get("arbitr:current_snat_ip")
        current_ip = _v.decode("utf-8") if _v else ""
    except Exception:
        current_ip = ""

    if cooldown.is_active(current_ip):
        u = cooldown.until(current_ip)
        logger.info("kad_monitor_one_case: cooldown(%s) until %s — отказ", current_ip, u)
        _log_check(
            case, ArbitrCheckLog.STATE_CAPTCHA,
            notes=f"IP {current_ip or '?'} приостановлен (cooldown до {u:%d.%m %H:%M} МСК)",
        )
        from django.core.cache import cache  # noqa: WPS433
        cache.delete(f"arbitr:active_task:{case.id}")
        return {"case_id": str(case.id), "result": "cooldown", "until": u.isoformat()}

    try:
        try:
            with KadSession() as kad:
                if case.status == ArbitrCase.STATUS_SEARCHING:
                    result = _search_one(kad, case, runner_ip=current_ip)  # str
                elif case.status == ArbitrCase.STATUS_MONITORING:
                    result = _parse_one(kad, case, runner_ip=current_ip)["result"]
                else:
                    _log_check(
                        case, ArbitrCheckLog.STATE_ERROR,
                        notes=f"Ручной запуск недоступен для статуса {case.status}",
                    )
                    return {"error": "wrong_status", "status": case.status}
        except KadCaptchaRequired as exc:
            _log_check(case, ArbitrCheckLog.STATE_CAPTCHA, notes=exc.page_url)
            handle_captcha(case, page_url=exc.page_url, ip=current_ip)
            result = "captcha"

        return {"case_id": str(case.id), "result": result}
    finally:
        # Сигнал «таск завершился» для UI-поллера в views.case_card_partial.
        # Тот же ключ что устанавливает views.case_run.
        from django.core.cache import cache  # noqa: WPS433 — local
        cache.delete(f"arbitr:active_task:{case.id}")
