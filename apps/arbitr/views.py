"""Views для UI мониторинга арбитражных дел."""
import time
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.models import Employee
from apps.core.permissions import is_admin
from apps.crm.models import Service
from apps.crm import client_log

from .models import ArbitrCase, ArbitrCheckLog
from .tasks import kad_monitor_one_case

# Пока работают beat-расписания каждый час — оба автотаска ходят раз в час
# (если попадают в work-window 18:00–08:00). Сильно перебарщивать с
# точностью не надо — это просто подсказка пользователю в UI.
NEXT_CHECK_INTERVAL = timedelta(hours=1)

# Если последний лог моложе LOG_FRESH_SECONDS — считаем что воркер активен и
# UI поллит лог раз в 4с. Иначе поллинг останавливается (сервер не присылает
# hx-trigger в партиале).
LOG_FRESH_SECONDS = 60


def _log_is_fresh(case: "ArbitrCase") -> bool:
    last = case.check_logs.first()
    if not last:
        return False
    return (timezone.now() - last.ts).total_seconds() < LOG_FRESH_SECONDS


def _can_manage(user):
    """Управление мониторингом дела: админы + юристы/АУ (вкладка «Суд» карточки БФЛ)."""
    from apps.procedure.permissions import can_access_procedures
    return is_admin(user) or can_access_procedures(user)


@login_required
@require_POST
def mark_iskotpravlen(request, service_id):
    """Создаёт ArbitrCase(status='searching') для услуги. На время отладки
    доступно только админам — потом этот вход переедет в отдельную страницу
    сотрудников отдела сбора документов."""
    if not _can_manage(request.user):
        return HttpResponse("forbidden", status=403)
    service = get_object_or_404(Service, pk=service_id)
    if hasattr(service, "arbitr_case"):
        return render(request, "arbitr/_case_block.html", {
            "case": service.arbitr_case, "service": service,
        })
    emp = Employee.objects.filter(user=request.user).first()
    case = ArbitrCase.objects.create(
        service=service, started_by=emp,
        status=ArbitrCase.STATUS_SEARCHING,
    )
    client_log.record_action(
        service.client, "claim_filed",
        employee=emp,
        comment=(
            f"Иск отправлен в суд. Запущен мониторинг дела на kad.arbitr.ru "
            f"(услуга {service.name.short_name if service.name else '—'})"
        ),
    )
    # Канбан-карточка ждёт компактный бейдж (chip), а полная карточка
    # услуги — расширенный блок. Различаем по параметру partial.
    if request.GET.get("partial") == "chip":
        return render(request, "arbitr/_case_chip.html", {"case": case})
    return render(request, "arbitr/_case_block.html", {
        "case": case, "service": service,
    })


@login_required
@require_POST
def confirm_case(request, case_id):
    """Сотрудник вписывает случае номер дела + ссылку → переход в monitoring."""
    case = get_object_or_404(ArbitrCase, pk=case_id)
    if not _can_manage(request.user):
        return HttpResponse("forbidden", status=403)
    case_number = (request.POST.get("case_number") or "").strip()
    kad_url = (request.POST.get("kad_url") or "").strip()
    if not case_number or not kad_url:
        return HttpResponseBadRequest("Нужны номер дела и ссылка на kad")
    case.case_number = case_number
    case.kad_url = kad_url
    case.status = ArbitrCase.STATUS_MONITORING
    case.save(update_fields=["case_number", "kad_url", "status", "updated_at"])
    client_log.record_action(
        case.service.client, "claim_filed",
        employee=Employee.objects.filter(user=request.user).first(),
        comment=f"Подтверждено арбитражное дело №{case_number} — {kad_url}",
    )
    return render(request, "arbitr/_case_block.html", {
        "case": case, "service": case.service,
    })


@login_required
@require_POST
def court_start(request, service_id):
    """Вкладка «Суд»: включить мониторинг дела. Если переданы номер дела + ссылка
    kad — сразу MONITORING; иначе SEARCHING (ищем по ФИО). Идемпотентно."""
    if not _can_manage(request.user):
        return HttpResponse("forbidden", status=403)
    service = get_object_or_404(Service, pk=service_id)
    emp = Employee.objects.filter(user=request.user).first()
    case = getattr(service, "arbitr_case", None)
    created = case is None
    if created:
        case = ArbitrCase.objects.create(
            service=service, started_by=emp, status=ArbitrCase.STATUS_SEARCHING,
        )
    case_number = (request.POST.get("case_number") or "").strip()
    kad_url = (request.POST.get("kad_url") or "").strip()
    if case_number and kad_url:
        case.case_number = case_number
        case.kad_url = kad_url
        case.status = ArbitrCase.STATUS_MONITORING
        case.save(update_fields=["case_number", "kad_url", "status", "updated_at"])
    elif case.status == ArbitrCase.STATUS_PAUSED:
        case.status = (ArbitrCase.STATUS_MONITORING
                       if (case.case_number and case.kad_url) else ArbitrCase.STATUS_SEARCHING)
        case.save(update_fields=["status", "updated_at"])
    if created:
        client_log.record_action(
            service.client, "claim_filed", employee=emp,
            comment=("Включён мониторинг дела на kad.arbitr.ru"
                     + (f" (№ {case_number})" if case_number else " — поиск по ФИО")),
        )
    return render(request, "arbitr/_case_block.html", {"case": case, "service": service})


@login_required
def case_block(request, service_id):
    """HTMX-партиал для отрисовки блока «Арбитражное дело» в карточке услуги."""
    service = get_object_or_404(Service, pk=service_id)
    case = getattr(service, "arbitr_case", None)
    return render(request, "arbitr/_case_block.html", {
        "case": case, "service": service,
    })


# ============================================================================
# Сервисная страница мониторинга
# ============================================================================

def _estimate_next_check_at(case: ArbitrCase):
    """Грубая оценка следующего захода парсера.

    Beat-расписание сейчас ~раз в час, но автотаск работает только в окне
    18:00–08:00 MSK. Возвращаем «last + час» если попадает в окно, иначе
    следующее открытие окна (18:00 ближайшего вечера).
    """
    base = case.last_check_at or case.created_at or timezone.now()
    candidate = base + NEXT_CHECK_INTERVAL
    local = timezone.localtime(candidate)
    hour = local.hour
    # Окно работы 18:00–08:00 (см. tasks.WORK_WINDOW_*).
    if 8 <= hour < 18:
        # Перепрыгиваем дневной gap → 18:00 этого же дня.
        candidate = local.replace(hour=18, minute=0, second=0, microsecond=0)
    return candidate


def _annotate_cases(qs):
    """Подмешивает к queryset'у case-список счётчики (events, attachments,
    last_log_state) — чтобы UI не делал N+1."""
    return (
        qs.select_related(
            "service__client", "service__region", "started_by__user",
        )
        .annotate(
            events_count=Count("events", distinct=True),
            attachments_count=Count(
                "events__attachments",
                filter=Q(events__attachments__isnull=False),
                distinct=True,
            ),
            attachments_downloaded=Count(
                "events__attachments",
                filter=Q(events__attachments__stored_file__isnull=False),
                distinct=True,
            ),
        )
    )


def _last_log_state(case: ArbitrCase) -> str:
    last = case.check_logs.first()
    return last.state if last else ""


def _case_pane_context(case):
    case.next_check_at = _estimate_next_check_at(case)
    case.last_log_state = _last_log_state(case)
    events = (
        case.events
        .prefetch_related("attachments")
        .order_by("-event_date", "-parsed_at")[:200]
    )
    logs = list(case.check_logs.all()[:50])
    return {
        "case": case,
        "events": events,
        "logs": logs,
        "poll_active": _log_is_fresh(case),
    }


@login_required
def dashboard(request):
    """Split-layout: слева список дел, справа панель выбранного дела."""
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)
    searching = list(_annotate_cases(
        ArbitrCase.objects.filter(status=ArbitrCase.STATUS_SEARCHING)
    ).order_by("-created_at"))
    monitoring = list(_annotate_cases(
        ArbitrCase.objects.filter(status=ArbitrCase.STATUS_MONITORING)
    ).order_by("-last_check_at"))
    paused = list(_annotate_cases(
        ArbitrCase.objects.filter(
            status__in=[ArbitrCase.STATUS_PAUSED, ArbitrCase.STATUS_CLOSED],
        )
    ).order_by("-updated_at")[:30])

    for case in searching + monitoring + paused:
        case.next_check_at = _estimate_next_check_at(case)
        case.last_log_state = _last_log_state(case)

    # Если в URL ?case=<uuid> — сразу выбран и развёрнут.
    selected_pane = None
    selected_id = request.GET.get("case", "").strip()
    if selected_id:
        selected = next(
            (c for c in searching + monitoring + paused if str(c.id) == selected_id),
            None,
        )
        if selected is None:
            try:
                selected = _annotate_cases(ArbitrCase.objects.all()).get(pk=selected_id)
                selected.next_check_at = _estimate_next_check_at(selected)
                selected.last_log_state = _last_log_state(selected)
            except (ArbitrCase.DoesNotExist, ValueError):
                selected = None
        if selected is not None:
            selected_pane = _case_pane_context(selected)

    return render(request, "arbitr/dashboard.html", {
        "searching": searching,
        "monitoring": monitoring,
        "paused": paused,
        "selected_pane": selected_pane,
        "selected_case_id": selected_id if selected_pane else "",
    })


@login_required
def case_detail(request, case_id):
    """Деталка дела.

    HTMX (`HX-Request: true`) → партиал `_case_pane.html` для swap'а
    в правую панель dashboard'а. Full-page (прямой URL) → редирект на
    `/arbitr/?case=<uuid>` → dashboard сам встроит pane.
    """
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(
        _annotate_cases(ArbitrCase.objects.all()),
        pk=case_id,
    )
    if not request.headers.get("HX-Request"):
        return HttpResponseRedirect(f"/arbitr/?case={case.id}")
    return render(
        request, "arbitr/partials/_case_pane.html", _case_pane_context(case),
    )


def _active_task_cache_key(case_id) -> str:
    """Ключ кэша для отслеживания активного celery-task'а по делу.

    Значение: {"task_id": str, "started_at": float}. TTL 10 мин (хватит
    на самый длинный парсинг + PDF). tasks.kad_monitor_one_case в finally
    блоке делает cache.delete по этому же ключу — сигнал «таск завершился».
    """
    return f"arbitr:active_task:{case_id}"


def _parse_progress_ctx(case):
    """Контекст прогресс-бара парсинга: фраза-этап и % по времени с запуска."""
    active = cache.get(_active_task_cache_key(case.id)) or {}
    elapsed = int(time.time() - active.get("started_at", time.time())) if active else 0
    is_search = case.status == ArbitrCase.STATUS_SEARCHING
    if elapsed < 4:
        phase = "Подключаемся к kad.arbitr.ru…"
    elif elapsed < 12:
        phase = "Ищем дело по ФИО…" if is_search else "Открываем карточку дела…"
    elif elapsed < 22:
        phase = "Скачиваем информацию по делу…"
    else:
        phase = "Скачиваем файлы (судебные акты)…"
    return {"service": case.service, "case": case,
            "phase": phase, "pct": min(92, 8 + int(elapsed * 3.5)), "elapsed": elapsed}


@login_required
@require_POST
def case_run(request, case_id):
    """Ручной запуск парсера. Сохраняет task_id в кэш — для poll'инга.

    block=1 → вернуть прогресс-блок (для встроенной вкладки «Суд»), иначе —
    дашбордную карточку. Повторный запуск при активном таске — отдаёт прогресс
    (block) или 409 (карточка), второй параллельный таск не стартует.
    """
    if not _can_manage(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(ArbitrCase, pk=case_id)
    as_block = bool(request.POST.get("block"))
    key = _active_task_cache_key(case.id)
    if cache.get(key):
        if as_block:
            return render(request, "arbitr/_case_block_progress.html", _parse_progress_ctx(case))
        return HttpResponse("Парсинг уже идёт", status=409)
    result = kad_monitor_one_case.delay(str(case.id))
    cache.set(key, {
        "task_id": result.id,
        "started_at": time.time(),
        "user": request.user.username,
    }, timeout=600)
    ArbitrCheckLog.objects.create(
        case=case, state=ArbitrCheckLog.STATE_OK,
        notes=f"Ручной запуск инициирован (user={request.user.username})",
    )
    if as_block:
        return render(request, "arbitr/_case_block_progress.html", _parse_progress_ctx(case))
    return _render_case_card(request, case)


@login_required
def case_block_status(request, case_id):
    """Поллинг прогресса для встроенного блока: пока таск активен — прогресс-блок;
    завершился — актуальный _case_block.html (поллинг останавливается)."""
    case = get_object_or_404(ArbitrCase, pk=case_id)
    if cache.get(_active_task_cache_key(case.id)):
        return render(request, "arbitr/_case_block_progress.html", _parse_progress_ctx(case))
    resp = render(request, "arbitr/_case_block.html", {"service": case.service, "case": case})
    resp["HX-Trigger"] = "courtParseDone"
    return resp


@login_required
@require_POST
def case_run_abort(request, case_id):
    """Прерывает активный ручной парсинг.

    Получает task_id из кэша, делает AsyncResult.revoke(terminate=True),
    чистит кэш, пишет в лог. Если активного таска нет — просто триггерит
    HX-Refresh (UI рассинхронизировался, перерендеримся).
    """
    if not _can_manage(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(ArbitrCase, pk=case_id)
    key = _active_task_cache_key(case.id)
    active = cache.get(key)
    if active:
        from celery.result import AsyncResult  # noqa: WPS433 — local
        try:
            AsyncResult(active["task_id"]).revoke(
                terminate=True, signal="SIGTERM",
            )
        except Exception:  # noqa: BLE001 — best-effort
            pass
        cache.delete(key)
        ArbitrCheckLog.objects.create(
            case=case, state=ArbitrCheckLog.STATE_ERROR,
            notes=f"Парсинг прерван пользователем (user={request.user.username})",
        )
    # Встроенная вкладка «Суд» — вернуть перерисованный блок (без HX-Refresh).
    if request.POST.get("stay"):
        return render(request, "arbitr/_case_block.html", {"case": case, "service": case.service})
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@login_required
@require_POST
def case_toggle_pause(request, case_id):
    """Переключает дело PAUSED ↔ SEARCHING/MONITORING.

    Автотаски `kad_monitor_pending` / `kad_monitor_case` уже фильтруют
    по статусу — PAUSED-дела автоматически выпадают из батча.
    `kad_monitor_one_case` (ручной запуск) тоже отказывает на PAUSED.

    При возобновлении статус выбирается по наличию case_number/kad_url:
    есть → MONITORING (этап 2), нет → SEARCHING (этап 1).

    HX-Refresh: true → dashboard перезагружается, дело попадает в нужную
    секцию sidebar'а с новым статусом.
    """
    if not _can_manage(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(ArbitrCase, pk=case_id)
    if case.status == ArbitrCase.STATUS_PAUSED:
        if case.case_number and case.kad_url:
            case.status = ArbitrCase.STATUS_MONITORING
        else:
            case.status = ArbitrCase.STATUS_SEARCHING
        note = f"Возобновлено (user={request.user.username})"
    else:
        case.status = ArbitrCase.STATUS_PAUSED
        note = f"Приостановлено (user={request.user.username})"
    case.save(update_fields=["status", "updated_at"])
    ArbitrCheckLog.objects.create(
        case=case, state=ArbitrCheckLog.STATE_OK, notes=note,
    )
    # Встроенная вкладка «Суд» — вернуть перерисованный блок (без HX-Refresh).
    if request.POST.get("stay"):
        return render(request, "arbitr/_case_block.html", {"case": case, "service": case.service})
    if request.headers.get("HX-Request"):
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    return HttpResponseRedirect(f"/arbitr/?case={case.id}")


@login_required
def case_card_partial(request, case_id):
    """HTMX-партиал карточки одного дела.

    Используется и для swap'а после ручного запуска, и для self-polling'а
    карточки во время активного парсинга (`?polling=1`).

    Если `?polling=1` И активного таска в кэше уже нет → парсер завершился,
    отвечаем HX-Refresh чтобы полностью перерендерить страницу (sidebar,
    хронология, счётчики events_count — всё обновится).
    """
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(
        _annotate_cases(ArbitrCase.objects.all()),
        pk=case_id,
    )
    is_polling = request.GET.get("polling") == "1"
    active = cache.get(_active_task_cache_key(case.id))
    if is_polling and not active:
        # Парсер завершился — триггерим полную перезагрузку.
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    return _render_case_card(request, case)


@login_required
def case_log_partial(request, case_id):
    """HTMX-партиал лога проверок (для детальной)."""
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(ArbitrCase, pk=case_id)
    logs = list(case.check_logs.all()[:50])
    return render(request, "arbitr/partials/_case_log.html", {
        "case": case, "logs": logs,
        "poll_active": _log_is_fresh(case),
    })


def _render_case_card(request, case):
    case.next_check_at = _estimate_next_check_at(case)
    case.last_log_state = _last_log_state(case)
    # Активный таск — выводим прогресс-UI и self-polling в шаблоне.
    active = cache.get(_active_task_cache_key(case.id))
    if active:
        case.active_task = active
        case.active_task_elapsed = int(time.time() - active.get("started_at", 0))
    return render(request, "arbitr/partials/_case_card.html", {"case": case})


@login_required
@require_POST
def case_confirm_hit(request, case_id, hit_index):
    """Сотрудник выбрал «своё» дело из найденных кандидатов.

    Берёт hit из case.search_hits[hit_index], выставляет case_number/kad_url,
    переводит case в MONITORING, очищает search_hits, пишет ClientEvent.
    Возвращает HX-Redirect на ту же страницу — нужна полная перезагрузка,
    т.к. меняется status и весь блок карточки рендерится по-другому.
    """
    if not _can_manage(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(ArbitrCase, pk=case_id)
    if case.status != ArbitrCase.STATUS_SEARCHING:
        return HttpResponseBadRequest("Подтверждение возможно только для status=searching")
    hits = case.search_hits or []
    if not (0 <= hit_index < len(hits)):
        return HttpResponseBadRequest(f"Кандидата с индексом {hit_index} нет")
    hit = hits[hit_index]
    case_number = (hit.get("case_number") or "").strip()
    kad_url = (hit.get("kad_url") or "").strip()
    if not case_number or not kad_url:
        return HttpResponseBadRequest("У кандидата нет case_number/kad_url")

    case.case_number = case_number
    case.kad_url = kad_url
    case.court_name = (hit.get("court_name") or "").strip()
    case.status = ArbitrCase.STATUS_MONITORING
    case.search_hits = []
    case.search_hits_at = None
    case.save(update_fields=[
        "case_number", "kad_url", "court_name", "status",
        "search_hits", "search_hits_at", "updated_at",
    ])

    emp = Employee.objects.filter(user=request.user).first()
    client_log.record_action(
        case.service.client, "claim_filed",
        employee=emp,
        comment=(
            f"Подтверждено арбитражное дело №{case_number} (из найденных "
            f"парсером). Перевод в мониторинг карточки."
        ),
    )
    ArbitrCheckLog.objects.create(
        case=case, state=ArbitrCheckLog.STATE_OK,
        notes=f"Сотрудник подтвердил дело {case_number} — переводим в MONITORING",
    )
    # stay=1 (из вкладки «Суд» карточки процедуры) — перерисовать блок дела на
    # месте, без редиректа на dashboard арбитража.
    if request.POST.get("stay") or request.GET.get("stay"):
        return render(request, "arbitr/_case_block.html",
                      {"service": case.service, "case": case})
    # После confirm весь блок страницы рендерится по-другому (нет
    # «кандидатов», есть инстанции/события). Перезагружаем dashboard с
    # выбранным делом в URL — sidebar тоже обновится с новым статусом.
    redirect_url = f"/arbitr/?case={case.id}"
    if request.headers.get("HX-Request"):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = redirect_url
        return response
    return HttpResponseRedirect(redirect_url)


# ============================================================================
# Поиск в шапке
# ============================================================================

SEARCH_GROUP_LIMIT = 20


@login_required
def arbitr_search(request):
    """HTMX-партиал результатов поиска по делам/услугам.

    Группирует:
      1. SEARCHING-кейсы (приоритет)
      2. MONITORING-кейсы
      3. Остальные — PAUSED/CLOSED-кейсы + Service БФЛ без ArbitrCase

    Фильтр Q-OR по ФИО / case_number / region. Лимит 20 на группу.
    """
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)

    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        # Пустой партиал → dropdown скрывается (CSS :empty).
        return HttpResponse("")

    # Группы из ?groups=<g1>&groups=<g2> (фильтр HTMX-чекбоксов).
    # Если параметра вообще нет — по умолчанию все три группы (нет фильтра).
    groups_list = request.GET.getlist("groups")
    if groups_list:
        active_groups = set(g.strip() for g in groups_list if g.strip())
    else:
        active_groups = {"searching", "monitoring", "other"}

    # Поиск по токенам: split q по пробелам, КАЖДОЕ слово должно матчить
    # хоть одно поле (last_name/first_name/patronymic/case_number/region).
    # Так «Иванов Иван» находит клиента с last_name=«Иванов», first_name=«Иван»
    # — слово «Иванов» матчит фамилию, слово «Иван» матчит имя.
    tokens = q.split()
    case_filter = Q()
    for tok in tokens:
        case_filter &= (
            Q(case_number__icontains=tok)
            | Q(service__region__name__icontains=tok)
            | Q(service__client__last_name__icontains=tok)
            | Q(service__client__first_name__icontains=tok)
            | Q(service__client__patronymic__icontains=tok)
        )
    case_qs = (
        ArbitrCase.objects
        .filter(case_filter)
        .select_related(
            "service__client", "service__region", "service__common_status",
        )
    )

    searching = (
        list(
            case_qs.filter(status=ArbitrCase.STATUS_SEARCHING)
            .order_by("-last_check_at", "-created_at")[:SEARCH_GROUP_LIMIT]
        )
        if "searching" in active_groups
        else []
    )
    monitoring = (
        list(
            case_qs.filter(status=ArbitrCase.STATUS_MONITORING)
            .order_by("-last_check_at", "-created_at")[:SEARCH_GROUP_LIMIT]
        )
        if "monitoring" in active_groups
        else []
    )
    other_cases = []
    services_no_case = []
    if "other" in active_groups:
        other_cases = list(
            case_qs.filter(status__in=[
                ArbitrCase.STATUS_PAUSED, ArbitrCase.STATUS_CLOSED,
            ]).order_by("-last_check_at", "-updated_at")[:SEARCH_GROUP_LIMIT]
        )
        # Услуги БФЛ без ArbitrCase — тот же tokenized search.
        service_filter = Q()
        for tok in tokens:
            service_filter &= (
                Q(client__last_name__icontains=tok)
                | Q(client__first_name__icontains=tok)
                | Q(client__patronymic__icontains=tok)
                | Q(region__name__icontains=tok)
            )
        services_no_case = list(
            Service.objects
            .filter(name__short_name__icontains="БФЛ", arbitr_case__isnull=True)
            .filter(service_filter)
            .select_related("client", "region", "common_status")
            .order_by("-created_at")[:SEARCH_GROUP_LIMIT]
        )

    return render(request, "arbitr/partials/_search_results.html", {
        "q": q,
        "searching": searching,
        "monitoring": monitoring,
        "other_cases": other_cases,
        "services_no_case": services_no_case,
        "other_total": len(other_cases) + len(services_no_case),
        "total": (
            len(searching) + len(monitoring)
            + len(other_cases) + len(services_no_case)
        ),
    })


@login_required
@require_POST
def case_create_searching(request, service_id):
    """Создаёт ArbitrCase(SEARCHING) на услуге БФЛ.

    Если на услуге уже есть ArbitrCase — просто возвращает HX-Redirect
    на него (idempotent). Иначе создаёт новый SEARCHING + ClientEvent
    «иск отправлен в суд» (вариация существующего mark_iskotpravlen flow).
    """
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)
    service = get_object_or_404(Service, pk=service_id)
    if hasattr(service, "arbitr_case"):
        case = service.arbitr_case
    else:
        emp = Employee.objects.filter(user=request.user).first()
        case = ArbitrCase.objects.create(
            service=service, started_by=emp,
            status=ArbitrCase.STATUS_SEARCHING,
        )
        ArbitrCheckLog.objects.create(
            case=case, state=ArbitrCheckLog.STATE_OK,
            notes=f"Поставлено на мониторинг через поиск (user={request.user.username})",
        )
    redirect_url = f"/arbitr/?case={case.id}"
    if request.headers.get("HX-Request"):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = redirect_url
        return response
    return HttpResponseRedirect(redirect_url)


@login_required
def parser_status(request):
    """Real-time панель работы парсера для /arbitr/.

    HTMX-полит каждые 5 сек. Партиал показывает: текущее состояние
    (работает / длинная пауза / капча), что парсит сейчас, счётчик до
    30-мин перерыва, статистику за 24ч, последние 5 успешных кейсов.
    """
    from apps.arbitr import cooldown
    from collections import Counter
    from django.utils.dateparse import parse_datetime

    now = timezone.now()
    # Per-IP cooldown: {ip: until_datetime}
    cooldown_by_ip = cooldown.all_active()
    BREAK_EVERY = 8

    # ── Per-runner state (3 параллельных контейнера arbitr-runner a/b/c) ──
    # Каждый runner имеет свой Lock/Throttle/Counter/CurrentCase в Redis и
    # назначенный rotator'ом outbound-IP (`arbitr:runner_ip:<id>`).
    RUNNERS = ["a", "b", "c", "d"]

    # IP назначения от rotator — пишутся без Django-префикса :1:, читаем напрямую.
    runner_ip_by_id = {}
    try:
        import redis as _redis  # noqa: WPS433
        from django.conf import settings as _settings  # noqa: WPS433
        _r = _redis.Redis.from_url(_settings.REDIS_URL)
        for rid in RUNNERS:
            v = _r.get(f"arbitr:runner_ip:{rid}")
            runner_ip_by_id[rid] = v.decode("utf-8") if v else ""
    except Exception:
        for rid in RUNNERS:
            runner_ip_by_id[rid] = ""

    runners = []
    for rid in RUNNERS:
        out_ip = runner_ip_by_id[rid]
        # throttle TTL
        thr_ttl = None
        thr_val = cache.get(f"arbitr:smart_throttle_until:{rid}")
        if thr_val:
            end_dt = parse_datetime(thr_val)
            if end_dt:
                thr_ttl = int((end_dt - now).total_seconds())
                if thr_ttl <= 0:
                    thr_ttl = None
        # parse_count
        cnt = int(cache.get(f"arbitr:smart_parse_count:{rid}") or 0)
        # current case
        cur_id = cache.get(f"arbitr:smart_current_case:{rid}")
        cur_case = None
        if cur_id:
            cur_case = (
                ArbitrCase.objects
                .select_related("service__client")
                .filter(pk=cur_id)
                .first()
            )
        # state-string
        ip_cooldown_until = cooldown_by_ip.get(out_ip) if out_ip else None
        if ip_cooldown_until:
            state_r = "captcha"
            label_r = f"IP {out_ip} в капче до {timezone.localtime(ip_cooldown_until):%H:%M}"
        elif not out_ip:
            state_r = "disabled"
            label_r = "Не активен (нет IP в этом окне)"
        elif thr_ttl and thr_ttl > 600:
            state_r = "break"
            label_r = f"Длинная пауза, осталось {thr_ttl//60}м"
        elif cur_id and not thr_ttl:
            state_r = "working"
            label_r = "Парсит"
        elif thr_ttl:
            state_r = "throttle"
            if thr_ttl >= 60:
                label_r = f"Пауза {thr_ttl//60}м {thr_ttl%60:02d}с"
            else:
                label_r = f"Пауза {thr_ttl}с"
        else:
            state_r = "idle"
            label_r = "Готов"
        runners.append({
            "id": rid,
            "out_ip": out_ip,
            "state": state_r,
            "label": label_r,
            "current_case": cur_case,
            "parse_count": cnt,
            "throttle_ttl": thr_ttl,
        })

    # Глобальное «есть ли хоть кто-то парсит сейчас»
    any_working = any(r["state"] == "working" for r in runners)
    any_active = any(r["out_ip"] and r["state"] != "captcha" for r in runners)
    n_captcha = sum(1 for r in runners if r["state"] == "captcha")
    if any_working:
        state = "working"
        n = sum(1 for r in runners if r["state"] == "working")
        state_label = f"Парсит ({n}/{len(runners)})"
    elif n_captcha and not any_active:
        state = "captcha"
        state_label = f"Все активные IP в капче ({n_captcha})"
    elif not any_active and not n_captcha:
        state = "idle"
        state_label = "Все runner'ы выключены (нет активных IP в этом окне)"
    else:
        state = "idle"
        state_label = "Ожидание"

    log_24h = ArbitrCheckLog.objects.filter(ts__gte=now - timedelta(hours=24))
    stats = Counter(log_24h.values_list("state", flat=True))

    last_parsed = list(
        ArbitrCase.objects
        .filter(last_check_ok=True)
        .select_related("service__client")
        .order_by("-last_check_at")[:5]
    )

    ready_parse = (
        ArbitrCase.objects
        .filter(status=ArbitrCase.STATUS_MONITORING)
        .filter(Q(next_parse_at__isnull=True) | Q(next_parse_at__lte=now))
        .count()
    )
    ready_search = (
        ArbitrCase.objects
        .filter(status=ArbitrCase.STATUS_SEARCHING)
        .filter(Q(next_search_at__isnull=True) | Q(next_search_at__lte=now))
        .count()
    )

    # Расписание IP-ротации (МСК) — должно совпадать со скриптом
    # ops/arbitr-snat-rotate.sh.
    SCHEDULE = [
        ("45.90.35.187",  21,  5),  # «через полночь» — обрабатывается ниже
        ("31.128.40.116",  5, 15),
        ("45.12.239.248",  9, 17),
        ("109.172.47.2",  11, 20),
        ("45.84.225.250",  0,  8),
    ]
    msk_hour = timezone.localtime().hour

    def _is_active(start, end, h):
        if start < end:
            return start <= h < end
        # окно «через полночь»
        return h >= start or h < end

    # rotator (host-side bash скрипт) пишет ключ напрямую в Redis через
    # `redis-cli SET arbitr:current_snat_ip …` — БЕЗ Django-префикса `:1:`.
    # Django cache добавил бы префикс при .get() и не нашёл бы значение,
    # поэтому читаем низкоуровнево через redis-py.
    current_ip = ""
    try:
        import redis as _redis  # noqa: WPS433
        from django.conf import settings as _settings  # noqa: WPS433
        _r = _redis.Redis.from_url(_settings.REDIS_URL)
        _v = _r.get("arbitr:current_snat_ip")
        current_ip = _v.decode("utf-8") if _v else ""
    except Exception:
        current_ip = ""
    ip_rows = []
    for ip, s, e in SCHEDULE:
        active_now = _is_active(s, e, msk_hour)
        cd_until = cooldown_by_ip.get(ip)
        ip_rows.append({
            "ip": ip,
            "start": s,
            "end": e,
            "active_now": active_now,
            "is_current": (ip == current_ip),
            "cooldown_until": cd_until,
        })

    ctx = {
        "state": state,
        "state_label": state_label,
        "runners": runners,
        "break_every": BREAK_EVERY,
        "cooldown_by_ip": cooldown_by_ip,
        "ok_24h": stats.get("ok", 0),
        "nothing_24h": stats.get("nothing", 0),
        "error_24h": stats.get("error", 0),
        "captcha_24h": stats.get("captcha", 0),
        "ready_parse": ready_parse,
        "ready_search": ready_search,
        "last_parsed": last_parsed,
        "ip_rows": ip_rows,
        "current_ip": current_ip,
        "msk_hour": msk_hour,
    }
    return render(request, "arbitr/partials/_parser_status.html", ctx)
