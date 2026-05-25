from datetime import date, datetime, timedelta, time as dtime
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db.models import Q

from apps.core.models import Employee
from apps.crm.models import Client
from apps.crm.models import ClientEvent
from .models import Consultation, ConsultationResult

SCHEDULE_HOURS = list(range(9, 19))
SCHEDULE_DAYS  = 14


def _current_employee(request):
    try:
        return Employee.objects.select_related("user", "department").get(user=request.user)
    except Employee.DoesNotExist:
        return None


@login_required
def schedule(request):
    emp = _current_employee(request)
    consultant_id = request.GET.get("consultant") or ""

    all_employees = Employee.objects.filter(is_active=True).select_related("user").order_by("user__last_name")

    if consultant_id:
        consultant = get_object_or_404(Employee, pk=consultant_id, is_active=True)
    elif emp and emp.role == "consultant":
        consultant = emp
    else:
        consultant = all_employees.filter(role="consultant").first()

    if consultant:
        consultant_id = str(consultant.pk)

    offset = int(request.GET.get("offset") or 0)
    today  = date.today() + timedelta(weeks=offset)
    start_date = today - timedelta(days=today.weekday())
    days = [start_date + timedelta(days=i) for i in range(SCHEDULE_DAYS)]

    rows = []
    c_map = {}
    if consultant:
        dt_from = timezone.make_aware(datetime.combine(days[0],  dtime(0, 0)))
        dt_to   = timezone.make_aware(datetime.combine(days[-1], dtime(23, 59)))
        for c in Consultation.objects.filter(
            consultant=consultant,
            datetime_start__range=(dt_from, dt_to),
        ).exclude(status="transferred").select_related("client", "booked_by", "result"):
            ld = timezone.localtime(c.datetime_start)
            c_map[(ld.date(), ld.hour)] = c

        today_real = date.today()
        now_hour   = datetime.now().hour
        for h in SCHEDULE_HOURS:
            cells = []
            for d in days:
                con = c_map.get((d, h))
                cells.append({
                    "date": d,
                    "hour": h,
                    "dt":   f"{d.strftime('%Y-%m-%d')}T{h:02d}:00:00",
                    "consultation": con,
                    "is_past": d < today_real or (d == today_real and h < now_hour),
                })
            rows.append({"hour": h, "cells": cells})

    consultants = all_employees

    return render(request, "consultations/schedule.html", {
        "consultant":    consultant,
        "consultant_id": consultant_id,
        "consultants":   consultants,
        "days":          days,
        "rows":          rows,
        "offset":        offset,
        "current_emp":   emp,
        "today":         date.today(),
    })


@login_required
def history(request):
    emp           = _current_employee(request)
    consultant_id = request.GET.get("consultant") or ""
    result_id     = request.GET.get("result") or ""
    status        = request.GET.get("status") or ""
    date_from     = request.GET.get("date_from") or ""
    date_to       = request.GET.get("date_to") or ""

    qs = Consultation.objects.exclude(status="free").select_related(
        "consultant__user", "client", "booked_by__user", "result"
    ).order_by("-datetime_start")

    if emp and emp.role in ("consultant", "lawyer", "arbitration") and not request.user.is_superuser:
        qs = qs.filter(consultant=emp)
    elif consultant_id:
        qs = qs.filter(consultant_id=consultant_id)

    if result_id:
        qs = qs.filter(result_id=result_id)
    if status:
        qs = qs.filter(status=status)
    if date_from:
        qs = qs.filter(datetime_start__date__gte=date_from)
    if date_to:
        qs = qs.filter(datetime_start__date__lte=date_to)

    consultants = Employee.objects.filter(
        is_active=True,
        role__in=("consultant", "lawyer", "arbitration", "managing_partner"),
    ).select_related("user").order_by("user__last_name")
    results = ConsultationResult.objects.filter(is_active=True)

    return render(request, "consultations/history.html", {
        "consultations": qs[:500],
        "consultants": consultants,
        "results": results,
        "filter_consultant": consultant_id,
        "filter_result": result_id,
        "filter_status": status,
        "filter_date_from": date_from,
        "filter_date_to": date_to,
        "current_emp": emp,
    })


@login_required
def book_modal(request):
    consultant_id = request.GET.get("consultant")
    dt_str        = request.GET.get("dt")
    consultant    = get_object_or_404(Employee, pk=consultant_id, is_active=True)
    return render(request, "consultations/partials/book_modal.html", {
        "consultant": consultant, "dt": dt_str,
    })


@login_required
@require_POST
def book(request):
    from django.utils.dateparse import parse_datetime
    emp           = _current_employee(request)
    consultant_id = request.POST.get("consultant_id")
    client_id     = request.POST.get("client_id")
    dt_str        = request.POST.get("datetime_start")
    comment       = (request.POST.get("comment") or "").strip()
    consultant    = get_object_or_404(Employee, pk=consultant_id, is_active=True)
    client        = get_object_or_404(Client, pk=client_id)
    dt_naive      = parse_datetime(dt_str)
    if not dt_naive:
        return HttpResponseBadRequest("Неверный формат даты")
    dt = timezone.make_aware(dt_naive) if timezone.is_naive(dt_naive) else dt_naive
    if Consultation.objects.filter(consultant=consultant, datetime_start=dt, status="booked").exists():
        return HttpResponseBadRequest("Это время уже занято")
    Consultation.objects.create(
        consultant=consultant, client=client, booked_by=emp,
        datetime_start=dt, status="booked", comment=comment,
    )
    ClientEvent.objects.create(
        client      = client,
        event_type  = "consultation_booked",
        description = f"Записан на консультацию к {consultant.user.last_name} {consultant.user.first_name}",
        new_value   = dt.strftime("%d.%m.%Y %H:%M"),
        employee    = emp,
    )
    resp = HttpResponse("")
    resp["HX-Trigger"] = "consultationChanged"
    return resp


@login_required
def result_modal(request, pk):
    consultation = get_object_or_404(Consultation, pk=pk)
    results      = ConsultationResult.objects.filter(is_active=True)
    return render(request, "consultations/partials/result_modal.html", {
        "consultation": consultation, "results": results,
    })


@login_required
@require_POST
def set_result(request, pk):
    emp                   = _current_employee(request)
    consultation          = get_object_or_404(Consultation, pk=pk)
    result_id             = request.POST.get("result_id") or None
    consultant_notes      = (request.POST.get("consultant_notes") or "").strip()
    status                = request.POST.get("status") or "done"
    old_status            = consultation.status
    result_obj            = ConsultationResult.objects.filter(pk=result_id).first() if result_id else None
    consultation.result           = result_obj
    consultation.consultant_notes = consultant_notes
    consultation.status           = status
    consultation.save(update_fields=["result", "consultant_notes", "status", "updated_at"])
    if consultation.client:
        status_labels = {"done": "Проведена", "booked": "Записан", "cancelled": "Отменена"}
        parts = [f"{consultation.datetime_start.strftime('%d.%m.%Y %H:%M')}"]
        if result_obj:
            parts.append(f"итог: {result_obj.name}")
        if status != old_status:
            parts.append(f"статус: {status_labels.get(status, status)}")
        ClientEvent.objects.create(
            client      = consultation.client,
            event_type  = "consultation_result",
            description = f"Консультация у {consultation.consultant.user.last_name} {consultation.consultant.user.first_name} — {', '.join(parts)}",
            new_value   = result_obj.name if result_obj else "",
            employee    = emp,
        )
    resp = HttpResponse("")
    resp["HX-Trigger"] = "consultationChanged"
    return resp


@login_required
def move_modal(request, pk):
    consultation      = get_object_or_404(Consultation, pk=pk)
    new_dt            = request.GET.get("datetime_start", "")
    new_consultant_id = request.GET.get("consultant_id") or str(consultation.consultant_id)
    return render(request, "consultations/partials/move_reason_modal.html", {
        "consultation":      consultation,
        "new_dt":            new_dt,
        "new_consultant_id": new_consultant_id,
    })


@login_required
@require_POST
def move_confirm(request, pk):
    from django.utils.dateparse import parse_datetime
    emp           = _current_employee(request)
    consultation  = get_object_or_404(Consultation, pk=pk)
    dt_str            = request.POST.get("datetime_start")
    consultant_id     = request.POST.get("consultant_id") or str(consultation.consultant_id)
    transfer_reason   = (request.POST.get("transfer_reason") or "").strip()
    dt_naive          = parse_datetime(dt_str)
    if not dt_naive:
        return HttpResponseBadRequest("Неверный формат даты")
    dt         = timezone.make_aware(dt_naive) if timezone.is_naive(dt_naive) else dt_naive
    consultant = get_object_or_404(Employee, pk=consultant_id)
    if Consultation.objects.filter(consultant=consultant, datetime_start=dt, status="booked").exists():
        return HttpResponseBadRequest("Это время уже занято")
    new_consultation = Consultation.objects.create(
        consultant    = consultant,
        client        = consultation.client,
        booked_by     = consultation.booked_by,
        datetime_start= dt,
        status        = "booked",
        comment       = consultation.comment,
    )
    consultation.status          = "transferred"
    consultation.transfer_reason = transfer_reason
    consultation.transferred_to  = new_consultation
    consultation.save(update_fields=["status", "transfer_reason", "transferred_to", "updated_at"])
    if consultation.client:
        old_dt = consultation.datetime_start.strftime("%d.%m.%Y %H:%M")
        new_dt_str = dt.strftime("%d.%m.%Y %H:%M")
        ClientEvent.objects.create(
            client      = consultation.client,
            event_type  = "consultation_transferred",
            description = f"Консультация перенесена с {old_dt} на {new_dt_str}. Причина: {transfer_reason}",
            old_value   = old_dt,
            new_value   = new_dt_str,
            employee    = emp,
        )
    resp = HttpResponse("")
    resp["HX-Trigger"] = "consultationChanged"
    return resp


@login_required
def client_search(request):
    q = (request.GET.get("q") or "").strip()
    clients = []
    if len(q) >= 2:
        clients = Client.objects.filter(
            Q(last_name__icontains=q) | Q(first_name__icontains=q) |
            Q(phone__icontains=q) | Q(phones__phone__icontains=q)
            | Q(username__icontains=q)
        ).distinct().order_by("last_name", "first_name")[:15]
    return render(request, "consultations/partials/client_search.html", {"clients": clients, "q": q})


@login_required
def edit_modal(request, pk):
    from django.utils.dateparse import parse_datetime
    emp          = _current_employee(request)
    consultation = get_object_or_404(Consultation, pk=pk)
    if request.method == "POST":
        dt_str     = request.POST.get("datetime_start")
        comment    = (request.POST.get("comment") or "").strip()
        old_dt_str = consultation.datetime_start.strftime("%d.%m.%Y %H:%M")
        changed    = []
        if dt_str:
            dt_naive = parse_datetime(dt_str)
            if dt_naive:
                dt = timezone.make_aware(dt_naive) if timezone.is_naive(dt_naive) else dt_naive
                if dt != consultation.datetime_start:
                    changed.append(f"время: {old_dt_str} → {dt.strftime('%d.%m.%Y %H:%M')}")
                    consultation.datetime_start = dt
        if comment != consultation.comment:
            changed.append("комментарий изменён")
        consultation.comment = comment
        consultation.save(update_fields=["datetime_start", "comment", "updated_at"])
        if consultation.client and changed:
            ClientEvent.objects.create(
                client      = consultation.client,
                event_type  = "consultation_edited",
                description = f"Консультация изменена: {'; '.join(changed)}",
                employee    = emp,
            )
        resp = HttpResponse("")
        resp["HX-Trigger"] = "consultationChanged"
        return resp
    return render(request, "consultations/partials/edit_modal.html", {"consultation": consultation})


@login_required
def result_reference(request):
    items = ConsultationResult.objects.all()
    return render(request, "consultations/partials/result_reference.html", {"items": items})


@login_required
def result_reference_form(request, pk=None):
    from .forms import ConsultationResultForm
    obj  = ConsultationResult.objects.filter(pk=pk).first() if pk else None
    if request.method == "POST":
        form = ConsultationResultForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            resp = HttpResponse("")
            resp["HX-Trigger"] = "reloadConsultationResults"
            return resp
    else:
        form = ConsultationResultForm(instance=obj)
    return render(request, "consultations/partials/result_form_modal.html", {"form": form, "obj": obj})


@login_required
@require_POST
def result_reference_delete(request, pk):
    ConsultationResult.objects.filter(pk=pk).delete()
    resp = HttpResponse("")
    resp["HX-Trigger"] = "reloadConsultationResults"
    return resp
