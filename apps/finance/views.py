"""
Views финансового учёта:

* Справочники (типы расходов/доходов + счета прихода/расхода) — права
  references_access (admin, head_dep, superuser).
* Модалка «Финансы и расчёты» — открыта всем, кто видит карточку клиента.
* Создание/редактирование/удаление платежей — отдельные права из permissions.py.
"""
from decimal import Decimal

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.views import is_references_access
from apps.crm.models import Client

from . import forms, models
from .permissions import can_delete_finance, can_edit_finance, require_delete, require_edit


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

    rows = []
    if "in" in kinds:
        for p in payments_qs.filter(direction="in"):
            rows.append({
                "kind": "in", "obj": p,
                "date": p.payment_date, "title": (p.income_type.name if p.income_type else "—"),
                "amount": p.amount_in or Decimal("0"),
                "status": p.get_payment_form_display(),
                "account": str(p.incoming_account) if p.incoming_account else "",
                "comments": p.comments,
                "service": p.service,
            })
    if "out" in kinds:
        for p in payments_qs.filter(direction="out"):
            rows.append({
                "kind": "out", "obj": p,
                "date": p.payment_date, "title": (p.expense_type.name if p.expense_type else "—"),
                "amount": p.amount_out or Decimal("0"),
                "status": p.get_payment_form_display(),
                "account": str(p.outgoing_account) if p.outgoing_account else "",
                "comments": p.comments,
                "service": p.service,
            })
    if "charge" in kinds:
        for c in charges_qs:
            rows.append({
                "kind": "charge", "obj": c,
                "date": c.due_date, "title": c.title,
                "amount": c.amount,
                "status": c.get_status_display(),
                "account": "",
                "comments": c.comments,
                "service": c.service,
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

    return {
        "client": client,
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
    })


@login_required
@require_delete
@require_POST
def payment_delete(request, client_id, payment_id):
    client = get_object_or_404(Client, pk=client_id)
    payment = get_object_or_404(models.Payment, pk=payment_id, client=client)
    payment.delete()
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = "reloadFinance"
    return resp
