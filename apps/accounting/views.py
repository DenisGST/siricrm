"""Views раздела «Бухгалтерский учёт» — рабочее место бухгалтера.

Панель грузится в #content-area сайдбар-пунктом (use_htmx), повторяет chrome
главной. Три вкладки:
  • Банк            — мониторинг источников входящих (выписка р/с + эквайринг);
  • Уведомления     — очередь разнесения входящих платежей (привязка вручную);
  • Внесённые платежи — реестр проведённых (привязанных) платежей.
"""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q, Sum
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.permissions import get_employee
from apps.crm.models import Client, Service
from apps.finance.models import (
    Charge, IncomeType, IncomingAccount, PAYMENT_FORM_CHOICES, Payment,
)

from . import integrations, services
from .models import AcquiringPrepay, IncomingPayment, SourcePoll
from .permissions import require_accounting

log = logging.getLogger(__name__)


def _unbound_count() -> int:
    return IncomingPayment.objects.filter(status=IncomingPayment.STATUS_NEW).count()


@login_required
@require_accounting
def panel(request):
    return render(request, "accounting/panel.html", {"unbound_count": _unbound_count()})


# ── Вкладка «Банк» — мониторинг источников ──────────────────────────────────

def _source_state(source: str, enabled: bool, mode: str) -> dict:
    configured = integrations.is_configured(source)
    last = SourcePoll.objects.filter(source=source).order_by("-created_at").first()
    last_ok = SourcePoll.objects.filter(source=source, ok=True).order_by("-created_at").first()
    next_at = None
    if mode == "poll" and last_ok:
        next_at = last_ok.created_at + timedelta(hours=settings.ACCOUNTING_POLL_MIN_INTERVAL_HOURS)
    return {
        "source": source,
        "mode": mode,  # poll (выписка) | webhook (эквайринг)
        "enabled": enabled,
        "configured": configured,
        # выписка активна при включённом гейте; эквайринг — как только настроен терминал
        "active": (enabled and configured) if mode == "poll" else configured,
        "last": last,
        "last_ok": last_ok,
        "next_at": next_at,
        "new_last": last_ok.created if last_ok else 0,
    }


@login_required
@require_accounting
def tab_bank(request):
    statement = _source_state(IncomingPayment.SOURCE_STATEMENT, settings.ACCOUNTING_STATEMENT_POLL_ENABLED, "poll")
    acquiring = _source_state(IncomingPayment.SOURCE_ACQUIRING, settings.ACCOUNTING_ACQUIRING_POLL_ENABLED, "webhook")
    acquiring["webhook_url"] = request.build_absolute_uri(reverse("accounting:acquiring_webhook"))
    today = timezone.now().date()
    ctx = {
        "statement": statement,
        "acquiring": acquiring,
        "polls": SourcePoll.objects.all()[:15],
        "queue": {
            "unbound": IncomingPayment.objects.filter(status=IncomingPayment.STATUS_NEW).count(),
            "unidentified": IncomingPayment.objects.filter(status=IncomingPayment.STATUS_UNIDENTIFIED).count(),
            "bound_today": IncomingPayment.objects.filter(
                status=IncomingPayment.STATUS_BOUND, bound_at__date=today,
            ).count(),
            "unbound_sum": IncomingPayment.objects.filter(
                status=IncomingPayment.STATUS_NEW,
            ).aggregate(s=Sum("amount"))["s"] or 0,
        },
        "min_interval": settings.ACCOUNTING_POLL_MIN_INTERVAL_HOURS,
    }
    return render(request, "accounting/partials/_tab_bank.html", ctx)


# ── Вкладка «Уведомления» — очередь разнесения ──────────────────────────────

@login_required
@require_accounting
def tab_notifications(request):
    status = request.GET.get("status", IncomingPayment.STATUS_NEW)
    source = request.GET.get("source", "all")
    kind = request.GET.get("kind", "all")  # all | direct | settlement
    qs = IncomingPayment.objects.select_related("bound_client")
    if status != "all":
        qs = qs.filter(status=status)
    if source != "all":
        qs = qs.filter(source=source)
    if kind == "direct":
        qs = qs.filter(is_settlement=False)
    elif kind == "settlement":
        qs = qs.filter(is_settlement=True)
    base = IncomingPayment.objects
    ctx = {
        "payments": qs[:200],
        "status": status,
        "source": source,
        "kind": kind,
        "counts": {
            "new": base.filter(status=IncomingPayment.STATUS_NEW).count(),
            "bound": base.filter(status=IncomingPayment.STATUS_BOUND).count(),
            "unidentified": base.filter(status=IncomingPayment.STATUS_UNIDENTIFIED).count(),
            "settlement": base.filter(is_settlement=True).count(),
        },
    }
    return render(request, "accounting/partials/_tab_notifications.html", ctx)


# ── Вкладка «Платежи» — реестр привязанных ──────────────────────────────────

@login_required
@require_accounting
def tab_payments(request):
    payments = (
        Payment.objects.filter(direction="in")
        .select_related("client", "charge", "income_type", "incoming_account")
        .order_by("-payment_date", "-created_at")[:100]
    )
    total_in = Payment.objects.filter(direction="in").aggregate(s=Sum("amount_in"))["s"] or 0
    return render(request, "accounting/partials/_tab_payments.html", {
        "payments": payments, "total_in": total_in,
    })


# ── Привязка ────────────────────────────────────────────────────────────────

def _bind_defaults(ip: IncomingPayment) -> dict:
    acc = services.default_incoming_account(ip.source)
    itype = services.default_income_type()
    return {
        "income_types": IncomeType.objects.filter(is_active=True).select_related("service_name"),
        "incoming_accounts": IncomingAccount.objects.filter(is_active=True),
        "payment_forms": PAYMENT_FORM_CHOICES,
        "default_account_id": acc.id if acc else "",
        "default_income_type_id": itype.id if itype else "",
        "default_form": "cashless",
        "default_date": ip.occurred_at.date().isoformat(),
    }


@login_required
@require_accounting
def payment_detail(request):
    """Платёжка: read-only карточка входящего платежа с данными из выписки."""
    ip = get_object_or_404(IncomingPayment.objects.select_related("bound_client"), pk=request.GET.get("ip"))
    created = []
    if ip.status == IncomingPayment.STATUS_BOUND:
        created = list(ip.created_payments.select_related("charge").all())
    return render(request, "accounting/partials/_payment_detail.html", {
        "ip": ip, "created_payments": created,
    })


@login_required
@require_accounting
def bind_modal(request):
    ip = get_object_or_404(IncomingPayment, pk=request.GET.get("ip"))
    ctx = {"ip": ip, **_bind_defaults(ip)}
    # Если уже привязан («Изменить») — сразу подгружаем клиента и его начисления.
    if ip.bound_client_id:
        ctx["preselected_client"] = ip.bound_client
        ctx["charges"] = _charges_for(ip, ip.bound_client)
    return render(request, "accounting/partials/_bind_modal.html", ctx)


@login_required
@require_accounting
def bind_client_search(request):
    q = (request.GET.get("q") or "").strip()
    ip_id = request.GET.get("ip", "")
    clients = Client.objects.none()
    if len(q) >= 2:
        # Поиск по нескольким словам: каждое слово ищем по всем полям (OR),
        # слова между собой — AND. Так «Иванов Иван» матчит фамилию И имя.
        flt = Q()
        for term in q.split():
            flt &= (
                Q(first_name__icontains=term)
                | Q(last_name__icontains=term)
                | Q(patronymic__icontains=term)
                | Q(phone__icontains=term)
                | Q(phones__phone__icontains=term)
                | Q(username__icontains=term)
            )
        clients = Client.objects.filter(flt).distinct().order_by("last_name", "first_name").prefetch_related(
            Prefetch(
                "services",
                queryset=Service.objects.select_related("name", "common_status", "region").order_by("-created_at"),
            )
        )[:15]
    return render(request, "accounting/partials/_bind_client_results.html", {
        "clients": clients, "ip_id": ip_id, "query": q,
    })


def _charges_for(ip: IncomingPayment, client) -> list:
    """Начисления клиента с жадным предзаполнением суммы платежа по сроку."""
    charges = list(
        Charge.objects.filter(client=client).select_related("service").order_by("due_date")
    )
    remaining = ip.amount
    for ch in charges:
        ch.rem = ch.remaining
        take = min(ch.rem, remaining) if (ch.rem and remaining > 0) else 0
        ch.prefill = take
        remaining -= take
    return charges


@login_required
@require_accounting
def bind_charges(request):
    ip = get_object_or_404(IncomingPayment, pk=request.GET.get("ip"))
    client = get_object_or_404(Client, pk=request.GET.get("client"))
    return render(request, "accounting/partials/_bind_clientblock.html", {
        "ip": ip, "client": client, "charges": _charges_for(ip, client),
    })


@login_required
@require_accounting
@require_POST
def bind_execute(request):
    ip = get_object_or_404(IncomingPayment, pk=request.POST.get("ip"))
    client = Client.objects.filter(pk=request.POST.get("client_id")).first()
    if not client:
        return HttpResponseBadRequest("Не выбран клиент")

    income_type = IncomeType.objects.filter(pk=request.POST.get("income_type")).first()
    incoming_account = IncomingAccount.objects.filter(pk=request.POST.get("incoming_account")).first()
    payment_form = request.POST.get("payment_form") or "cashless"
    payment_date = parse_date(request.POST.get("payment_date") or "") or ip.occurred_at.date()
    note = (request.POST.get("note") or "").strip()

    allocations = []
    if not request.POST.get("no_charge"):
        for key, val in request.POST.items():
            if not key.startswith("alloc_"):
                continue
            amount = services.parse_amount(val)
            if amount <= 0:
                continue
            charge = Charge.objects.filter(pk=key[len("alloc_"):], client=client).first()
            if charge:
                allocations.append((charge, amount))

    services.bind_incoming_payment(
        ip, employee=get_employee(request.user), client=client,
        allocations=allocations, income_type=income_type,
        incoming_account=incoming_account, payment_form=payment_form,
        payment_date=payment_date, note=note,
    )
    return HttpResponse(status=204, headers={"HX-Trigger": "acctQueueChanged"})


@login_required
@require_accounting
@require_POST
def mark_unidentified_view(request):
    ip = get_object_or_404(IncomingPayment, pk=request.POST.get("ip"))
    services.mark_unidentified(
        ip, employee=get_employee(request.user), note=(request.POST.get("note") or "").strip(),
    )
    return HttpResponse(status=204, headers={"HX-Trigger": "acctQueueChanged"})


@login_required
@require_accounting
@require_POST
def poll_now(request):
    # Только выписка р/с (эквайринг — приём через webhook).
    from .tasks import poll_incoming_source
    poll_incoming_source.delay(IncomingPayment.SOURCE_STATEMENT, force=True)
    return HttpResponse(status=204, headers={"HX-Trigger": "acctBankChanged"})


# ── Webhook эквайринга (приём нотификаций ТБанк) ─────────────────────────────

@csrf_exempt
@require_POST
def acquiring_webhook(request):
    """Приём нотификаций интернет-эквайринга ТБанк (тонко: проверка подписи +
    создание входящего платежа). 🛑 Тяжёлой работы тут нет — как у WhatsApp."""
    try:
        data = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("bad json")

    if not integrations.validate_acquiring_notification(data):
        scalar_keys = sorted(k for k, v in data.items() if not isinstance(v, (dict, list)))
        log.warning(
            "Эквайринг: неверная подпись (PaymentId=%s, Status=%s). Поля: %s",
            data.get("PaymentId"), data.get("Status"), scalar_keys,
        )
        return HttpResponse(status=403)

    if str(data.get("Status", "")) == "CONFIRMED":
        op = integrations.parse_acquiring_notification(data)
        if op["external_id"]:
            ip, created = IncomingPayment.objects.get_or_create(
                source=IncomingPayment.SOURCE_ACQUIRING, external_id=op["external_id"],
                defaults={
                    "occurred_at": op["occurred_at"], "amount": op["amount"],
                    "payer_name": op["payer_name"], "payer_phone": op["payer_phone"],
                    "purpose": op["purpose"], "order_id": op["order_id"], "raw": op["raw"],
                },
            )
            # Обогащаем ФИО/телефоном из prepay (страница оплаты прислала их до платежа).
            _enrich_from_prepay(ip, op["order_id"])
            # Журнал получения (для «Банк» — последняя нотификация).
            SourcePoll.objects.create(
                source=IncomingPayment.SOURCE_ACQUIRING, ok=True, found=1, created=int(created),
            )
    # ТБанк ждёт тело "OK" при успешной обработке.
    return HttpResponse("OK")


def _enrich_from_prepay(ip, order_id):
    if not order_id:
        return
    prepay = AcquiringPrepay.objects.filter(order_id=order_id).first()
    if not prepay:
        return
    fields = []
    if prepay.name and not ip.payer_name:
        ip.payer_name = prepay.name[:255]
        fields.append("payer_name")
    if prepay.phone and not ip.payer_phone:
        ip.payer_phone = prepay.phone[:32]
        fields.append("payer_phone")
    if fields:
        ip.save(update_fields=fields)
    if not prepay.matched:
        prepay.matched = True
        prepay.save(update_fields=["matched"])


@csrf_exempt
@require_POST
def acquiring_prepay(request):
    """Приём данных страницы оплаты ДО платежа: order_id + ФИО + телефон.
    Публичный (как вебхук); fire-and-forget с fo-y.ru (sendBeacon)."""
    order_id = (request.POST.get("order_id") or "").strip()
    if not order_id:
        return HttpResponseBadRequest("no order_id")
    AcquiringPrepay.objects.update_or_create(
        order_id=order_id[:128],
        defaults={
            "name": (request.POST.get("name") or "").strip()[:255],
            "phone": (request.POST.get("phone") or "").strip()[:32],
            "amount": services.parse_amount(request.POST.get("amount")) or None,
        },
    )
    return HttpResponse("OK")
