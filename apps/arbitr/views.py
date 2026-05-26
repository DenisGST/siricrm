"""Views для UI мониторинга арбитражных дел."""
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from apps.core.models import Employee
from apps.core.permissions import is_admin
from apps.crm.models import Service, ClientEvent

from .models import ArbitrCase


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
