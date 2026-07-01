"""Views раздела «Отчёты».

Панель грузится в #content-area сайдбар-пунктом (use_htmx), повторяет chrome
главной. Раздел расширяемый — отчёты добавляются вкладками.

Отчёт «Отдел продаж» — реестр входящих платежей-юруслуг (гонорар фирмы) за
месяц + бюджет отдела продаж. Поддерживает сортировку по столбцам и фильтр по
закреплённому за клиентом сотруднику (Client.employees).

Бюджет ОП начисляется по правилу на каждую операцию-поступление (S = «Сумма»,
юруслуги-часть строки):
  • S < 5000 ₽ → 0;
  • S ≥ 5000 ₽ и дата платежа < (дата введения 1-й процедуры + 8 мес) → 1000 ₽;
  • S ≥ 5000 ₽ и дата платежа ≥ (та же дата + 8 мес) → 400 ₽;
  • даты введения процедуры нет → считаем как «до 8 мес» (S ≥ 5000 → 1000 ₽).
«Дата введения первой процедуры» = минимальная Procedure.intro_date среди
процедур дела (BankruptcyCase) клиента, путь Payment.service → bankruptcy_case.
"""
import calendar
import datetime
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounting.models import IncomingPayment
from apps.core.models import Employee
from apps.core.permissions import get_employee
from apps.finance.models import Payment
from apps.procedure.models import BankruptcyCase

from .models import SalesBudget, SalesBudgetEntry
from .permissions import require_reports

ZERO = Decimal("0")
ACCRUAL_MIN_PAYMENT = Decimal("5000")   # порог суммы платежа
ACCRUAL_EARLY = Decimal("1000")         # до 8 мес с даты введения процедуры
ACCRUAL_LATE = Decimal("400")           # после 8 мес
ACCRUAL_MONTHS = 8

SORT_KEYS = {
    "fio": lambda o: o["client_fio"].lower(),
    "date": lambda o: o["date"],
    "decision_date": lambda o: o["decision_date"] or datetime.date.min,
    "amount_full": lambda o: o["amount_full"],
    "type": lambda o: (o["type"] or "").lower(),
    "purpose": lambda o: (o["purpose"] or "").lower(),
    "comments": lambda o: (o["comments"] or "").lower(),
    "amount": lambda o: o["amount"],
    "accrued": lambda o: o["accrued"] if o["accrued"] is not None else Decimal("-1"),
}
DEFAULT_SORT = "date"


@login_required
@require_reports
def panel(request):
    """Лендинг раздела «Отчёты» (вкладки)."""
    return render(request, "reports/panel.html", {})


# ── Вспомогательные ──────────────────────────────────────────────────────

def _parse_month(raw: str):
    """«YYYY-MM» → (year, month). Некорректный ввод → текущий месяц."""
    if raw:
        try:
            y, m = raw.split("-")
            y, m = int(y), int(m)
            if 1 <= m <= 12 and 2000 <= y <= 2100:
                return y, m
        except (ValueError, AttributeError):
            pass
    today = timezone.localdate()
    return today.year, today.month


def _client_fio(client) -> str:
    fio = " ".join(
        part for part in (client.last_name, client.first_name, client.patronymic) if part
    ).strip()
    return fio or str(client)


def _add_months(d: datetime.date, months: int) -> datetime.date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return datetime.date(y, m, day)


def _accrual(credited, pay_date, decision_date) -> Decimal:
    """Расчётное начисление в бюджет ОП по одной операции."""
    if credited is None or credited < ACCRUAL_MIN_PAYMENT:
        return ZERO
    if decision_date is None:
        return ACCRUAL_EARLY  # нет даты решения → считаем как «до 8 мес»
    threshold = _add_months(decision_date, ACCRUAL_MONTHS)
    return ACCRUAL_EARLY if pay_date < threshold else ACCRUAL_LATE


def _assigned_employees():
    """Сотрудники, закреплённые хотя бы за одним клиентом (для фильтра)."""
    qs = (
        Employee.objects
        .filter(clients__isnull=False, user__is_active=True)
        .select_related("user")
        .distinct()
        .order_by("user__last_name", "user__first_name")
    )
    out = []
    for e in qs:
        label = (e.user.get_full_name() or e.user.username).strip()
        out.append({"id": str(e.id), "label": label})
    return out


def _decision_dates(pays):
    """Карты дат введения ПЕРВОЙ процедуры дела (min Procedure.intro_date):
    {service_id: date} и {client_id: date} (fallback, если платёж без услуги)."""
    service_ids = {p.service_id for p in pays if p.service_id}
    client_ids = {p.client_id for p in pays}
    svc, cli = {}, {}
    if service_ids or client_ids:
        cases = (
            BankruptcyCase.objects
            .filter(Q(service_id__in=service_ids) | Q(service__client_id__in=client_ids))
            .select_related("service")
            .prefetch_related("procedures")
        )
        for case in cases:
            intro = [pr.intro_date for pr in case.procedures.all() if pr.intro_date]
            if not intro:
                continue
            d = min(intro)
            svc[case.service_id] = d
            cid = case.service.client_id
            if cli.get(cid) is None or d < cli[cid]:
                cli[cid] = d
    return svc, cli


def _compute_operations(year, month, employee_id=None):
    """Список операций-поступлений за месяц с расчётным начислением.

    employee_id — фильтр по закреплённому за клиентом сотруднику (Client.employees).
    Возвращает (month_start, ops, total_credited, total_full). ops — без «n»/«accrued»
    (проставляются на этапе рендера после сортировки/оверлея начислений).
    """
    month_start = datetime.date(year, month, 1)
    month_end = datetime.date(year, month, calendar.monthrange(year, month)[1])

    qs = Payment.objects.filter(
        direction="in",
        income_type__is_legal_services=True,
        payment_date__gte=month_start,
        payment_date__lte=month_end,
    )
    if employee_id:
        qs = qs.filter(client__employees=employee_id).distinct()
    pays = list(
        qs.select_related("client", "income_type", "incoming_account", "charge")
        .order_by("payment_date", "created_at")
    )

    # Родительская операция (accounting.IncomingPayment) → её сумма = «целиком».
    parent = {}
    pay_ids = [p.id for p in pays]
    if pay_ids:
        for ip_id, ip_amount, pay_id in IncomingPayment.objects.filter(
            created_payments__in=pay_ids
        ).values_list("id", "amount", "created_payments"):
            if pay_id is not None:
                parent[pay_id] = (ip_id, ip_amount or ZERO)

    svc_date, cli_date = _decision_dates(pays)

    # Группировка по операции-поступлению.
    groups = OrderedDict()
    for p in pays:
        if p.id in parent:
            op_id, op_amount = parent[p.id]
            key = ("ip", op_id)
        else:
            op_amount = p.amount_in or ZERO
            key = ("pay", p.id)
        g = groups.get(key)
        if g is None:
            g = {"first": p, "op_amount": op_amount, "credited": ZERO}
            groups[key] = g
        g["credited"] += (p.amount_in or ZERO)

    ops = []
    for g in groups.values():
        p = g["first"]
        dd = None
        if p.service_id and p.service_id in svc_date:
            dd = svc_date[p.service_id]
        elif p.client_id in cli_date:
            dd = cli_date[p.client_id]
        if p.charge_id and p.charge and p.charge.title:
            purpose = p.charge.title
        elif p.income_type_id and p.income_type:
            purpose = p.income_type.name
        else:
            purpose = ""
        # «Просрочка» = платёж позже, чем через 8 мес от даты введения процедуры
        # (ветка 400 ₽). Для подсветки суммы/даты платежа тёмно-красным.
        is_late = dd is not None and p.payment_date >= _add_months(dd, ACCRUAL_MONTHS)
        ops.append({
            "payment": p,
            "client_fio": _client_fio(p.client),
            "date": p.payment_date,
            "amount_full": g["op_amount"],
            "type": p.get_payment_form_display(),
            "purpose": purpose,
            "comments": p.comments,
            "amount": g["credited"],
            "decision_date": dd,
            "is_late": is_late,
            "computed": _accrual(g["credited"], p.payment_date, dd),
        })

    total_credited = sum((o["amount"] for o in ops), ZERO)
    total_full = sum((o["amount_full"] for o in ops), ZERO)
    return month_start, ops, total_credited, total_full


def _render_sales_tab(request, year, month, emp="", sort=DEFAULT_SORT, direction="asc"):
    employees = _assigned_employees()
    valid_ids = {e["id"] for e in employees}
    emp = emp if emp in valid_ids else ""
    sort = sort if sort in SORT_KEYS else DEFAULT_SORT
    direction = direction if direction in ("asc", "desc") else "asc"

    month_start, ops, total, total_full = _compute_operations(
        year, month, employee_id=(emp or None),
    )

    budget = (
        SalesBudget.objects.filter(month=month_start)
        .prefetch_related("entries").first()
    )
    entries = {e.payment_id: e.accrued for e in budget.entries.all()} if budget else {}

    total_accrued = ZERO
    for o in ops:
        acc = entries.get(o["payment"].id)
        o["accrued"] = acc  # None — если ещё не рассчитано/не вводилось
        if acc is not None:
            total_accrued += acc

    ops.sort(key=SORT_KEYS[sort], reverse=(direction == "desc"))
    for i, o in enumerate(ops, 1):
        o["n"] = i

    ctx = {
        "month_value": f"{year:04d}-{month:02d}",
        "rows": ops,
        "total": total,
        "total_full": total_full,
        "count": len(ops),
        "budget_total": budget.budget_total if budget else None,
        "calculated_at": budget.calculated_at if budget else None,
        "total_accrued": total_accrued,
        "employees": employees,
        "emp": emp,
        "sort": sort,
        "direction": direction,
    }
    return render(request, "reports/partials/_tab_sales.html", ctx)


# ── Вьюхи ────────────────────────────────────────────────────────────────

@login_required
@require_reports
def tab_sales(request):
    """Отчёт «Результаты работы отдела продаж» за выбранный месяц."""
    year, month = _parse_month(request.GET.get("month", ""))
    return _render_sales_tab(
        request, year, month,
        emp=(request.GET.get("emp") or "").strip(),
        sort=(request.GET.get("sort") or DEFAULT_SORT),
        direction=(request.GET.get("dir") or "asc"),
    )


@login_required
@require_reports
@require_POST
def budget_calculate(request):
    """«Рассчитать»: проставить расчётное начисление в строки и заполнить
    поле «Бюджет отдела продаж» (= сумма расчётных начислений).

    Считается по ВСЕМ операциям месяца (без фильтра сотрудника) — бюджет
    относится к отделу целиком. Отображение потом фильтруется/сортируется.
    """
    year, month = _parse_month(request.POST.get("month", ""))
    month_start, ops, _total, _full = _compute_operations(year, month)
    emp = get_employee(request.user)

    with transaction.atomic():
        budget, _ = SalesBudget.objects.get_or_create(month=month_start)
        keep = set()
        budget_total = ZERO
        for o in ops:
            p = o["payment"]
            SalesBudgetEntry.objects.update_or_create(
                budget=budget, payment=p,
                defaults={"computed": o["computed"], "accrued": o["computed"]},
            )
            keep.add(p.id)
            budget_total += o["computed"]
        # Удалить начисления по операциям, выпавшим из выборки.
        budget.entries.exclude(payment_id__in=keep).delete()
        budget.budget_total = budget_total
        budget.calculated_at = timezone.now()
        budget.calculated_by = emp
        budget.save()

    return _render_sales_tab(
        request, year, month,
        emp=(request.POST.get("emp") or "").strip(),
        sort=(request.POST.get("sort") or DEFAULT_SORT),
        direction=(request.POST.get("dir") or "asc"),
    )


@login_required
@require_reports
@require_POST
def budget_entry_save(request, payment_id):
    """Онлайн-правка поля «Начислено в бюджет ОП» по строке."""
    year, month = _parse_month(request.POST.get("month", ""))
    month_start = datetime.date(year, month, 1)
    emp = (request.POST.get("emp") or "").strip()
    payment = get_object_or_404(Payment, pk=payment_id)

    raw = (request.POST.get("value") or "").strip().replace(" ", "").replace(",", ".")
    try:
        value = Decimal(raw) if raw else ZERO
    except InvalidOperation:
        return HttpResponseBadRequest("Некорректное значение")
    if value < ZERO:
        value = ZERO

    with transaction.atomic():
        budget, _ = SalesBudget.objects.get_or_create(month=month_start)
        SalesBudgetEntry.objects.update_or_create(
            budget=budget, payment=payment, defaults={"accrued": value},
        )
        # «Итого начислено» — с учётом текущего фильтра сотрудника.
        entries = budget.entries.all()
        if emp:
            entries = entries.filter(payment__client__employees=emp)
        total_accrued = entries.aggregate(s=Sum("accrued"))["s"] or ZERO

    return render(request, "reports/partials/_budget_accrued_total.html",
                  {"total_accrued": total_accrued})
