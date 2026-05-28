"""Views для UI мониторинга арбитражных дел."""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.models import Employee
from apps.core.permissions import is_admin
from apps.crm.models import Service, ClientEvent

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


@login_required
@require_POST
def mark_iskotpravlen(request, service_id):
    """Создаёт ArbitrCase(status='searching') для услуги. На время отладки
    доступно только админам — потом этот вход переедет в отдельную страницу
    сотрудников отдела сбора документов."""
    if not is_admin(request.user):
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
    ClientEvent.objects.create(
        client=service.client, event_type="iskotpravlen",
        employee=emp,
        description=(
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
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)
    case_number = (request.POST.get("case_number") or "").strip()
    kad_url = (request.POST.get("kad_url") or "").strip()
    if not case_number or not kad_url:
        return HttpResponseBadRequest("Нужны номер дела и ссылка на kad")
    case.case_number = case_number
    case.kad_url = kad_url
    case.status = ArbitrCase.STATUS_MONITORING
    case.save(update_fields=["case_number", "kad_url", "status", "updated_at"])
    ClientEvent.objects.create(
        client=case.service.client, event_type="iskotpravlen",
        employee=Employee.objects.filter(user=request.user).first(),
        description=f"Подтверждено арбитражное дело №{case_number} — {kad_url}",
    )
    return render(request, "arbitr/_case_block.html", {
        "case": case, "service": case.service,
    })


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
        qs.select_related("service__client", "started_by__user")
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


@login_required
def dashboard(request):
    """Сервисная страница: список дел в двух секциях — поиск и мониторинг."""
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

    # Для каждого case считаем next_check_at и last_log_state (через свойства
    # модели не пробросить — annotation сложен; делаем в Python, так как
    # данных немного).
    for case in searching + monitoring + paused:
        case.next_check_at = _estimate_next_check_at(case)
        case.last_log_state = _last_log_state(case)

    return render(request, "arbitr/dashboard.html", {
        "searching": searching,
        "monitoring": monitoring,
        "paused": paused,
    })


@login_required
def case_detail(request, case_id):
    """Деталка дела: шапка + инстанции + события + лог."""
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(
        _annotate_cases(ArbitrCase.objects.all()),
        pk=case_id,
    )
    case.next_check_at = _estimate_next_check_at(case)
    case.last_log_state = _last_log_state(case)
    events = (
        case.events
        .prefetch_related("attachments")
        .order_by("-event_date", "-parsed_at")[:200]
    )
    logs = list(case.check_logs.all()[:50])
    return render(request, "arbitr/case_detail.html", {
        "case": case,
        "events": events,
        "logs": logs,
        "poll_active": _log_is_fresh(case),
    })


@login_required
@require_POST
def case_run(request, case_id):
    """Ручной запуск парсера для конкретного дела. Возвращает обновлённую карточку."""
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(ArbitrCase, pk=case_id)
    kad_monitor_one_case.delay(str(case.id))
    # Сразу пишем «manual run scheduled» в лог — чтобы поллер видел
    # что что-то происходит до того, как воркер реально стартует.
    ArbitrCheckLog.objects.create(
        case=case, state=ArbitrCheckLog.STATE_OK,
        notes=f"Ручной запуск инициирован (user={request.user.username})",
    )
    return _render_case_card(request, case)


@login_required
def case_card_partial(request, case_id):
    """HTMX-партиал карточки одного дела (для dashboard'а)."""
    if not is_admin(request.user):
        return HttpResponse("forbidden", status=403)
    case = get_object_or_404(
        _annotate_cases(ArbitrCase.objects.all()),
        pk=case_id,
    )
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
    return render(request, "arbitr/partials/_case_card.html", {"case": case})
