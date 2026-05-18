"""
Views финансового учёта:

* Справочники (типы расходов/доходов + счета прихода/расхода) — права
  references_access (admin, head_dep, superuser).
* Модалка «Финансы и расчёты» — открыта всем, кто видит карточку клиента.
* Создание/редактирование/удаление платежей — отдельные права из permissions.py.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.views import is_references_access
from apps.crm.models import Client, Service

from . import forms, models
from .permissions import (
    can_delete_charge, can_delete_finance, can_edit_finance,
    require_delete, require_edit,
)


# ────────────────────────────────────────────────────────────
# Справочники
# ────────────────────────────────────────────────────────────

@user_passes_test(is_references_access)
def references_expense_types(request):
    items = models.ExpenseType.objects.select_related("service_name").all()
    return render(request, "finance/partials/references_expense_types.html", {"items": items})


@user_passes_test(is_references_access)
def reference_expense_type_edit(request, pk=None):
    obj = get_object_or_404(models.ExpenseType, pk=pk) if pk else None
    if request.method == "POST":
        form = forms.ExpenseTypeForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadExpenseTypes"})
    else:
        form = forms.ExpenseTypeForm(instance=obj)
    return render(request, "finance/partials/expense_type_form_modal.html", {"form": form, "obj": obj})


@user_passes_test(is_references_access)
@require_POST
def reference_expense_type_delete(request, pk):
    obj = get_object_or_404(models.ExpenseType, pk=pk)
    if obj.payments.exists():
        return HttpResponse(
            f"Нельзя удалить: тип используется в {obj.payments.count()} платежах.",
            status=409,
        )
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadExpenseTypes"})


@user_passes_test(is_references_access)
def references_income_types(request):
    items = models.IncomeType.objects.select_related("service_name").all()
    return render(request, "finance/partials/references_income_types.html", {"items": items})


@user_passes_test(is_references_access)
def reference_income_type_edit(request, pk=None):
    obj = get_object_or_404(models.IncomeType, pk=pk) if pk else None
    if request.method == "POST":
        form = forms.IncomeTypeForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadIncomeTypes"})
    else:
        form = forms.IncomeTypeForm(instance=obj)
    return render(request, "finance/partials/income_type_form_modal.html", {"form": form, "obj": obj})


@user_passes_test(is_references_access)
@require_POST
def reference_income_type_delete(request, pk):
    obj = get_object_or_404(models.IncomeType, pk=pk)
    if obj.payments.exists():
        return HttpResponse(
            f"Нельзя удалить: тип используется в {obj.payments.count()} платежах.",
            status=409,
        )
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadIncomeTypes"})


@user_passes_test(is_references_access)
def references_incoming_accounts(request):
    items = models.IncomingAccount.objects.all()
    return render(request, "finance/partials/references_incoming_accounts.html", {"items": items})


@user_passes_test(is_references_access)
def reference_incoming_account_edit(request, pk=None):
    obj = get_object_or_404(models.IncomingAccount, pk=pk) if pk else None
    if request.method == "POST":
        form = forms.IncomingAccountForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadIncomingAccounts"})
    else:
        form = forms.IncomingAccountForm(instance=obj)
    return render(request, "finance/partials/incoming_account_form_modal.html", {"form": form, "obj": obj})


@user_passes_test(is_references_access)
@require_POST
def reference_incoming_account_delete(request, pk):
    obj = get_object_or_404(models.IncomingAccount, pk=pk)
    if obj.payments.exists():
        return HttpResponse(
            f"Нельзя удалить: счёт используется в {obj.payments.count()} платежах.",
            status=409,
        )
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadIncomingAccounts"})


@user_passes_test(is_references_access)
def references_outgoing_accounts(request):
    items = models.OutgoingAccount.objects.all()
    return render(request, "finance/partials/references_outgoing_accounts.html", {"items": items})


@user_passes_test(is_references_access)
def reference_outgoing_account_edit(request, pk=None):
    obj = get_object_or_404(models.OutgoingAccount, pk=pk) if pk else None
    if request.method == "POST":
        form = forms.OutgoingAccountForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadOutgoingAccounts"})
    else:
        form = forms.OutgoingAccountForm(instance=obj)
    return render(request, "finance/partials/outgoing_account_form_modal.html", {"form": form, "obj": obj})


@user_passes_test(is_references_access)
@require_POST
def reference_outgoing_account_delete(request, pk):
    obj = get_object_or_404(models.OutgoingAccount, pk=pk)
    if obj.payments.exists():
        return HttpResponse(
            f"Нельзя удалить: счёт используется в {obj.payments.count()} платежах.",
            status=409,
        )
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadOutgoingAccounts"})


# ────────────────────────────────────────────────────────────
# Модалка «Финансы и расчёты» (на карточке клиента)
# ────────────────────────────────────────────────────────────

ALL_KINDS = ("charge", "in", "out")

SORT_KEYS = {
    "date":    lambda r: r["date"],
    "kind":    lambda r: r["kind"],
    "title":   lambda r: (r["title"] or "").lower(),
    "service": lambda r: (r["service"].name.short_name if r["service"] else ""),
    "amount":  lambda r: r["amount"],
    "account": lambda r: r["account"] or "",
    "status":  lambda r: r["status"] or "",
}


def _finance_context(client, *, kinds=ALL_KINDS, sort="date", direction="desc"):
    """Собираем строки таблицы (платежи + начисления) и сводные цифры.

    `kinds` — какие группы строк показывать в таблице. На сводные цифры
    фильтр не влияет: пользователь должен видеть полную картину.
    """
    payments_qs = (
        models.Payment.objects
        .filter(client=client)
        .select_related("expense_type", "income_type", "incoming_account", "outgoing_account",
                        "service__name", "charge", "created_by__user")
    )
    charges_qs = (
        models.Charge.objects
        .filter(client=client)
        .select_related("service__name")
    )

    def _short_form(p):
        return {"cash": "нал.", "cashless": "б/нал."}.get(p.payment_form, p.payment_form or "")

    def _short_charge_status(c):
        return "опл." if c.status == "paid" else "не опл."

    def _emp_name(emp):
        if not emp:
            return ""
        full = emp.user.get_full_name() if emp.user_id else ""
        return full or (emp.user.username if emp.user_id else "")

    def _meta(obj):
        return {
            "created_by": _emp_name(getattr(obj, "created_by", None)),
            "created_at": obj.created_at,
            "updated_by": _emp_name(getattr(obj, "updated_by", None)),
            "updated_at": obj.updated_at,
        }

    rows = []
    if "in" in kinds:
        for p in payments_qs.filter(direction="in"):
            rows.append({
                "kind": "in", "obj": p,
                "date": p.payment_date, "title": (p.income_type.name if p.income_type else "—"),
                "amount": p.amount_in or Decimal("0"),
                "status": _short_form(p),
                "account": str(p.incoming_account) if p.incoming_account else "",
                "comments": p.comments,
                "service": p.service,
                "meta": _meta(p),
            })
    if "out" in kinds:
        for p in payments_qs.filter(direction="out"):
            rows.append({
                "kind": "out", "obj": p,
                "date": p.payment_date, "title": (p.expense_type.name if p.expense_type else "—"),
                "amount": p.amount_out or Decimal("0"),
                "status": _short_form(p),
                "account": str(p.outgoing_account) if p.outgoing_account else "",
                "comments": p.comments,
                "service": p.service,
                "meta": _meta(p),
            })
    if "charge" in kinds:
        for c in charges_qs:
            rows.append({
                "kind": "charge", "obj": c,
                "date": c.due_date, "title": c.title,
                "amount": c.amount,
                "status": _short_charge_status(c),
                "account": "",
                "comments": c.comments,
                "service": c.service,
                "meta": _meta(c),
            })

    if sort not in SORT_KEYS:
        sort = "date"
    direction = "asc" if direction == "asc" else "desc"
    # Стабильная сортировка: внутри одинакового ключа — по дате desc.
    rows.sort(key=lambda r: r["date"], reverse=True)
    rows.sort(key=SORT_KEYS[sort], reverse=(direction == "desc"))

    total_charges = charges_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    total_in = payments_qs.filter(direction="in").aggregate(s=Sum("amount_in"))["s"] or Decimal("0")
    total_out = payments_qs.filter(direction="out").aggregate(s=Sum("amount_out"))["s"] or Decimal("0")

    client_services = list(
        client.services.select_related("name").order_by("date_dogovor", "contract_seq")
    )

    return {
        "client": client,
        "client_services": client_services,
        "rows": rows,
        "filter_kinds": list(kinds),
        "show_charge": "charge" in kinds,
        "show_in": "in" in kinds,
        "show_out": "out" in kinds,
        "sort": sort,
        "dir": direction,
        "totals": {
            "charged": total_charges,
            "paid_in": total_in,
            "paid_out": total_out,
            "saldo_fact": total_in - total_out,
            "saldo_plan": total_charges - total_in,
        },
        "can_edit": can_edit_finance(client._user) if hasattr(client, "_user") else False,
        "can_delete": can_delete_finance(client._user) if hasattr(client, "_user") else False,
    }


@login_required
def finance_modal(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    client._user = request.user

    # Логика фильтра: hidden filter_applied=1 говорит, что запрос пришёл
    # из формы фильтра — тогда отсутствие kinds значит «всё снято». Без
    # маркера (первая загрузка модалки / reloadFinance) — показываем всё.
    raw = request.GET.getlist("kinds")
    valid = set(ALL_KINDS)
    if request.GET.get("filter_applied"):
        kinds = tuple(k for k in raw if k in valid)
    else:
        kinds = tuple(k for k in raw if k in valid) or ALL_KINDS

    sort = request.GET.get("sort", "date")
    direction = request.GET.get("dir", "desc")

    ctx = _finance_context(client, kinds=kinds, sort=sort, direction=direction)
    template = "finance/partials/finance_table.html" if request.GET.get("partial") \
        else "finance/partials/finance_modal.html"
    return render(request, template, ctx)


# ────────────────────────────────────────────────────────────
# Форма платежа (создание / редактирование)
# ────────────────────────────────────────────────────────────

@login_required
@require_edit
def payment_form_view(request, client_id, direction=None, payment_id=None):
    client = get_object_or_404(Client, pk=client_id)
    payment = get_object_or_404(models.Payment, pk=payment_id, client=client) if payment_id else None
    initial = {}
    if payment is None:
        initial["payment_date"] = timezone.localdate()
        initial["payment_form"] = "cashless"
        if direction in ("in", "out"):
            initial["direction"] = direction
        last_svc = client.services.order_by("-created_at").first()
        if last_svc:
            initial["service"] = last_svc.id

    if request.method == "POST":
        form = forms.PaymentForm(request.POST, instance=payment, client=client)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.client = client
            emp = getattr(request.user, "employee", None)
            if payment is None:
                obj.created_by = emp
            obj.updated_by = emp
            obj.save()
            # Возвращаем пустую строку — модалка с outerHTML-swap'ом исчезнет,
            # а событие reloadFinance перерисует таблицу в основной модалке.
            resp = HttpResponse("")
            resp["HX-Trigger"] = "reloadFinance"
            return resp
    else:
        form = forms.PaymentForm(instance=payment, client=client, initial=initial)

    return render(request, "finance/partials/payment_form_modal.html", {
        "form": form, "client": client, "payment": payment,
        "form_direction": (payment.direction if payment else (direction or "in")),
        "can_delete": can_delete_finance(request.user),
    })


# ────────────────────────────────────────────────────────────
# График платежей по услуге (генератор Charge-записей)
# ────────────────────────────────────────────────────────────

# Поля Service, которые редактируем через модалку графика.
SCHEDULE_FIELDS = (
    "legal_services_amount", "installment_months",
    "doc_collection", "postal_costs", "state_duty",
    "fu_fee", "procedure_costs", "additional_costs",
    "schedule_legal_offset", "schedule_fu_offset", "schedule_procedure_offset",
)


RU_MONTHS = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}


def _first_installment_date(date_start, offset_months):
    """10-е число месяца, который наступает после date_start + N месяцев.

    Если день получившейся даты > 10 — переносим на 10-е следующего месяца.
    """
    base = date_start + relativedelta(months=offset_months)
    if base.day > 10:
        base = base + relativedelta(months=1)
    return base.replace(day=10)


def _generate_charges(service):
    """Создаёт Charge-записи по параметрам, сохранённым в service.

    Юруслуги: первая дата = date_start + schedule_legal_offset мес, 10-е
    число (с переносом если день > 10), далее +1 месяц.
    ФУ: date_start + schedule_fu_offset мес.
    Расходы на процедуру: date_start + schedule_procedure_offset мес.
    Сбор док/Почтовые/Госпошлина: date_start + 7 дней.
    Доп. расходы: date_start + 60 дней.
    """
    new_charges = []
    date_d = service.date_start

    # Юруслуги — рассрочка
    if service.legal_services_amount and service.installment_months:
        monthly = (service.legal_services_amount / service.installment_months).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )
        first = _first_installment_date(date_d, service.schedule_legal_offset)
        for i in range(service.installment_months):
            due_date = first + relativedelta(months=i)
            month_year = f"{RU_MONTHS[due_date.month]} {due_date.year}"
            new_charges.append(models.Charge(
                client=service.client, service=service,
                due_date=due_date,
                title=f"Юруслуги, платёж {i + 1}/{service.installment_months} за {month_year}",
                amount=monthly,
                status="scheduled",
            ))

    # Платежи со смещением в днях (фиксированные).
    days_extras = [
        (service.doc_collection,    7,  "Сбор документов"),
        (service.postal_costs,      7,  "Почтовые расходы"),
        (service.state_duty,        7,  "Гос. пошлина"),
        (service.additional_costs, 60,  "Доп. расходы"),
    ]
    for amount, days_offset, title in days_extras:
        if amount and amount > 0:
            new_charges.append(models.Charge(
                client=service.client, service=service,
                due_date=date_d + timedelta(days=days_offset),
                title=title, amount=amount, status="scheduled",
            ))

    # Платежи со смещением в месяцах (настраиваемые).
    months_extras = [
        (service.fu_fee,          service.schedule_fu_offset,        "Вознаграждение ФУ"),
        (service.procedure_costs, service.schedule_procedure_offset, "Расходы на процедуру"),
    ]
    for amount, months_offset, title in months_extras:
        if amount and amount > 0:
            new_charges.append(models.Charge(
                client=service.client, service=service,
                due_date=date_d + relativedelta(months=months_offset),
                title=title, amount=amount, status="scheduled",
            ))

    models.Charge.objects.bulk_create(new_charges)
    return len(new_charges)


def _schedule_modal_ctx(service, *, error=None, success=None):
    charges = list(service.charges.order_by("due_date"))
    total = sum((c.amount for c in charges), Decimal("0"))
    return {
        "service": service,
        "existing_count": len(charges),
        "charges": charges,
        "charges_total": f"{total:,.2f}".replace(",", " "),
        "months_range": range(1, 25),
        "offset_range": range(0, 7),
        "date_start_value": service.date_start or timezone.localdate(),
        "error": error,
        "success": success,
    }


@login_required
@require_edit
def payment_schedule_modal(request, service_id):
    """Модалка «График платежей» в форме услуги.

    GET — рендерит форму + таблицу существующих Charge.
    POST — сохраняет параметры, при replace/append перегенерирует и
    возвращает ту же модалку с обновлённой таблицей (не закрывает её).
    """
    service = get_object_or_404(Service, pk=service_id)

    if request.method == "POST":
        try:
            for field in SCHEDULE_FIELDS:
                raw = request.POST.get(field, "").strip().replace(",", ".")
                if field == "installment_months":
                    setattr(service, field, max(1, min(24, int(raw or 1))))
                elif field in {"schedule_legal_offset", "schedule_fu_offset", "schedule_procedure_offset"}:
                    setattr(service, field, max(0, min(6, int(raw or 0))))
                else:
                    setattr(service, field, Decimal(raw or "0"))
            raw_date = request.POST.get("date_start", "").strip()
            if raw_date:
                from datetime import date
                service.date_start = date.fromisoformat(raw_date)
            service.save(update_fields=SCHEDULE_FIELDS + ("date_start",))
        except (ValueError, TypeError):
            return HttpResponse("Некорректные числовые значения или дата", status=400)

        if not service.date_start:
            return render(
                request, "finance/partials/payment_schedule_modal.html",
                _schedule_modal_ctx(service, error="Не указана дата начала оказания услуг — без неё нельзя построить график."),
            )

        strategy = request.POST.get("strategy", "save_only")
        success = None
        if strategy == "replace":
            service.charges.all().delete()
        if strategy in ("replace", "append"):
            with transaction.atomic():
                created = _generate_charges(service)
                # Пишем метаданные графика и contract_price = итого.
                emp = getattr(request.user, "employee", None)
                today = timezone.localdate()
                was_empty = service.schedule_date is None
                service.schedule_date = today
                if was_empty:
                    service.schedule_created_by = emp
                service.schedule_updated_by = emp
                total = service.charges.aggregate(s=Sum("amount"))["s"] or Decimal("0")
                service.contract_price = total
                service.save(update_fields=[
                    "schedule_date", "schedule_created_by", "schedule_updated_by",
                    "contract_price",
                ])
            success = f"Создано {created} начислени{'е' if created == 1 else ('я' if 1 < created < 5 else 'й')}. Цена договора: {total} руб."

        ctx = _schedule_modal_ctx(service, success=success)
        resp = render(request, "finance/partials/payment_schedule_modal.html", ctx)
        resp["HX-Trigger"] = "reloadFinance"
        return resp

    return render(
        request, "finance/partials/payment_schedule_modal.html",
        _schedule_modal_ctx(service),
    )


@login_required
@require_edit
def charge_edit(request, service_id, charge_id):
    """Редактирование одного начисления из модалки графика."""
    service = get_object_or_404(Service, pk=service_id)
    charge = get_object_or_404(models.Charge, pk=charge_id, service=service)

    if request.method == "POST":
        form = forms.ChargeForm(request.POST, instance=charge)
        if form.is_valid():
            obj = form.save(commit=False)
            emp = getattr(request.user, "employee", None)
            obj.updated_by = emp
            obj.save()
            resp = HttpResponse("")
            resp["HX-Trigger"] = "reloadSchedule, reloadFinance"
            return resp
    else:
        form = forms.ChargeForm(instance=charge)

    return render(request, "finance/partials/charge_form_modal.html", {
        "form": form, "charge": charge, "service": service,
        "can_delete": can_delete_charge(request.user, service),
    })


@login_required
@require_POST
def charge_delete(request, service_id, charge_id):
    service = get_object_or_404(Service, pk=service_id)
    if not can_delete_charge(request.user, service):
        return HttpResponse("Нет прав на удаление начисления", status=403)
    charge = get_object_or_404(models.Charge, pk=charge_id, service=service)
    charge.delete()
    resp = HttpResponse("")
    resp["HX-Trigger"] = "reloadSchedule, reloadFinance"
    return resp


@login_required
@require_delete
@require_POST
def payment_delete(request, client_id, payment_id):
    client = get_object_or_404(Client, pk=client_id)
    payment = get_object_or_404(models.Payment, pk=payment_id, client=client)
    payment.delete()
    # Пустая строка с 200 — модалка формы с hx-swap=outerHTML исчезнет,
    # а HX-Trigger перерисует таблицу финансов.
    resp = HttpResponse("")
    resp["HX-Trigger"] = "reloadFinance"
    return resp
