"""Рабочее место оператора колл-центра.

Две части:

* доска (канбан) — ``board`` + ленивые колонки ``column`` + перетаскивание
  ``card_move``;
* настройка колонок для админа — ``admin_columns`` и соседи, вкладка
  «Колл-центр» в Панели управления.

Колонки берутся из БД (``CallCenterColumn``), а не зашиты в шаблон, поэтому
их состав и порядок меняются без правки кода.
"""
from urllib.parse import quote

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.core.models import Employee
from apps.core.permissions import get_employee, is_admin, is_management
from apps.crm.models import Client, ClientEmployee

from .forms import BlockedPhoneForm, CallCenterColumnForm, CallResultForm
from .models import (BlockedPhone, CallCenterCard, CallCenterColumn,
                     CallOutcome, CallResult)
from .permissions import require_callcenter

# Столько карточек отдаём за раз; остальное догружается на скролл — как в
# колонке главного канбана (иначе после импорта из Bubble рендер вешает
# страницу).
PAGE_SIZE = 15


# Пресеты «Следующее действие» — минуты от текущего момента; tomorrow
# считается отдельно. Набор тот же, что у «Напомнить позже» в уведомлениях,
# чтобы у оператора не было двух разных наборов сроков.
ACTION_PRESETS = [
    ("15m", "Через 15 минут", 15),
    ("1h", "Через час", 60),
    ("3h", "Через 3 часа", 180),
    ("tomorrow", "Завтра, 10:00", None),
]
# Частые формулировки — заполняют поле в один клик.
ACTION_SUGGESTIONS = ["Позвонить", "Перезвонить", "Отправить договор",
                      "Напомнить о консультации"]


def _parse_action_at(raw: str):
    """<input type="datetime-local"> → aware datetime. → (dt | None, ok).

    🛑 Браузер отдаёт НАИВНОЕ local-время («2026-07-28T13:00»), поэтому
    привязываем его к текущей зоне (Europe/Moscow). Без make_aware Django
    в USE_TZ-проекте сохранил бы момент со сдвигом.
    """
    from django.utils.dateparse import parse_datetime

    raw = (raw or "").strip()
    if not raw:
        return None, True
    dt = parse_datetime(raw)
    if dt is None:
        return None, False
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt, True


# ────────────────────────── доска оператора ──────────────────────────

def resolve_owner(request):
    """Чей канбан показываем. → (значение фильтра, Employee | None).

    Значения: "" / "all" — все карточки, "free" — общий пул (никем не взяты),
    иначе id сотрудника.

    🛑 Чужой канбан открыт только руководству (``is_management``) — как в
    «Моём канбане» с ``?viewed_employee=``. Рядовой оператор, подставивший
    чужой id в адрес, увидит свой: подмену молча сводим к «мои», а не
    отдаём 403 — иначе устаревшая ссылка ломала бы доску.
    """
    raw = (request.GET.get("owner") or request.POST.get("owner") or "").strip()
    me = get_employee(request.user)
    if raw == "mine":
        return (str(me.pk) if me else "free"), me
    if raw in ("", "all", "free"):
        return raw, None
    if not is_management(request.user):
        return (str(me.pk) if me else "free"), me
    emp = Employee.objects.filter(pk=raw, is_active=True).select_related("user").first()
    if emp is None:
        return "", None
    return str(emp.pk), emp


def _visible_cards(request):
    """Карточки, к которым у пользователя есть доступ (по видимости клиента)."""
    return CallCenterCard.objects.select_related("client", "column").filter(
        client__in=Client.objects.visible_to(request.user))


def _cards_queryset(request):
    """Карточки доски, видимые пользователю, с учётом фильтров панели."""
    qs = CallCenterCard.objects.filter(
        client__in=Client.objects.visible_to(request.user),
    ).select_related("client", "column", "operator__user").prefetch_related(
        # для client.primary_employee без N+1 — как в колонке главного канбана
        Prefetch("client__client_employees",
                 queryset=ClientEmployee.objects.select_related("employee__user")),
    )

    q = (request.GET.get("q") or "").strip()
    if q:
        # Каждое слово запроса должно совпасть с одним из полей (AND по
        # словам, OR по полям) — как в фильтре главного канбана.
        for word in q.split():
            qs = qs.filter(
                Q(client__first_name__icontains=word)
                | Q(client__last_name__icontains=word)
                | Q(client__patronymic__icontains=word)
                | Q(client__phone__icontains=word)
                | Q(client__phones__phone__icontains=word)
            )
        qs = qs.distinct()

    employee_id = (request.GET.get("employee") or "").strip()
    if employee_id == "__none__":
        qs = qs.filter(client__employees__isnull=True)
    elif employee_id:
        qs = qs.filter(client__employees__id=employee_id).distinct()

    owner, _ = resolve_owner(request)
    if owner == "free":
        qs = qs.filter(operator__isnull=True)
    elif owner not in ("", "all"):
        qs = qs.filter(operator_id=owner)

    return qs


def _pending_context(request) -> dict:
    qs = (CallOutcome.objects.filter(employee=get_employee(request.user),
                                     filled_at__isnull=True)
          .order_by("-started_at"))
    return {"count": qs.count(), "latest": qs.first()}


@login_required
@require_callcenter
@never_cache
def board(request):
    """Доска колл-центра. Колонки — из справочника, карточки — лениво."""
    # 🛑 Прямой заход (F5, ссылка, возврат после логина) отдал бы голый
    # партиал без вёрстки и htmx — редиректим на дашборд с ?open=.
    if "HX-Request" not in request.headers:
        params = request.GET.urlencode()
        target = "/callcenter/" + (f"?{params}" if params else "")
        return redirect(f"/?open={quote(target, safe='')}")

    columns = list(CallCenterColumn.objects.filter(is_active=True))
    employees_all = (Employee.objects.filter(is_active=True)
                     .select_related("user")
                     .order_by("user__last_name", "user__first_name"))
    owner, owner_emp = resolve_owner(request)
    me = get_employee(request.user)
    return render(request, "callcenter/board.html", {
        "columns": columns,
        "employees_all": employees_all,
        # Выбор «чей канбан»: свои/пул/все + сотрудники (только руководству).
        "owner": (request.GET.get("owner") or "").strip(),
        "owner_emp": owner_emp,
        "can_view_others": is_management(request.user),
        "operators_all": [e for e in employees_all
                          if e.can_access_callcenter or (me and e.pk == me.pk)],
        "default_column": next((c for c in columns if c.is_default), None),
        "filter_q": (request.GET.get("q") or "").strip(),
        "filter_employee": (request.GET.get("employee") or "").strip(),
        "total_cards": CallCenterCard.objects.count(),
        # Звонки без записанного результата — долг оператора, видный сразу.
        **_pending_context(request),
    })


@login_required
@require_callcenter
@never_cache
def column(request, column_id):
    """Содержимое одной колонки (ленивая подгрузка + «показать ещё»)."""
    col = get_object_or_404(CallCenterColumn, pk=column_id)
    qs = _cards_queryset(request).filter(column=col).order_by("-moved_at")

    total = qs.count()
    try:
        offset = max(int(request.GET.get("offset") or 0), 0)
    except (TypeError, ValueError):
        offset = 0
    cards = list(qs[offset:offset + PAGE_SIZE])
    next_offset = offset + PAGE_SIZE

    return render(request, "callcenter/partials/_column_body.html", {
        "column": col,
        "cards": cards,
        "count": total,
        "offset": offset,
        "has_more": next_offset < total,
        "next_offset": next_offset,
        "remaining": max(total - next_offset, 0),
        "page_size": PAGE_SIZE,
        "over_limit": bool(col.wip_limit and total > col.wip_limit),
        "now": timezone.now(),
    })


@login_required
@require_callcenter
@require_POST
def card_move(request):
    """Перетаскивание карточки в другую колонку."""
    card = get_object_or_404(
        CallCenterCard.objects.filter(
            client__in=Client.objects.visible_to(request.user)),
        pk=request.POST.get("card"),
    )
    col = get_object_or_404(CallCenterColumn, pk=request.POST.get("column"), is_active=True)
    if card.column_id != col.pk:
        card.column = col
        card.moved_at = timezone.now()
        card.moved_by = get_employee(request.user)
        card.save(update_fields=["column", "moved_at", "moved_by", "updated_at"])
    return HttpResponse(status=204)


@login_required
@require_callcenter
def card_add_modal(request):
    """Модалка «Добавить клиента на доску» (поиск по ФИО/телефону)."""
    return render(request, "callcenter/partials/card_add_modal.html", {
        "default_column": CallCenterColumn.objects.filter(is_default=True, is_active=True).first(),
    })


@login_required
@require_callcenter
def card_add_search(request):
    """Живой поиск клиента для модалки добавления."""
    q = (request.GET.get("q") or "").strip()
    clients = []
    if len(q) >= 2:
        qs = Client.objects.visible_to(request.user)
        for word in q.split():
            qs = qs.filter(
                Q(first_name__icontains=word)
                | Q(last_name__icontains=word)
                | Q(patronymic__icontains=word)
                | Q(phone__icontains=word)
                | Q(phones__phone__icontains=word)
            )
        clients = list(qs.distinct().order_by("last_name", "first_name")[:15])
    on_board = set(
        CallCenterCard.objects.filter(client__in=[c.pk for c in clients])
        .values_list("client_id", flat=True)
    )
    return render(request, "callcenter/partials/card_add_results.html", {
        "clients": clients, "q": q, "on_board": on_board,
    })


@login_required
@require_callcenter
@require_POST
def card_add(request, client_id):
    """Кладёт клиента на доску — в колонку по умолчанию."""
    client = get_object_or_404(Client.objects.visible_to(request.user), pk=client_id)
    col = (CallCenterColumn.objects.filter(is_default=True, is_active=True).first()
           or CallCenterColumn.objects.filter(is_active=True).first())
    if col is None:
        return HttpResponse(
            '<div class="alert alert-error text-sm">Колонки не настроены — '
            'заведите их в Панели управления → «Колл-центр».</div>',
            status=409,
        )
    CallCenterCard.objects.get_or_create(
        client=client, defaults={"column": col, "moved_by": get_employee(request.user)},
    )
    return HttpResponse(headers={"HX-Trigger": "callcenterRefresh"})


@login_required
@require_callcenter
def card_action_modal(request, pk):
    """Модалка «Следующее действие» по карточке."""
    card = get_object_or_404(_visible_cards(request), pk=pk)
    return render(request, "callcenter/partials/card_action_modal.html", {
        "card": card,
        "presets": ACTION_PRESETS,
        "suggestions": ACTION_SUGGESTIONS,
    })


@login_required
@require_callcenter
@require_POST
def card_action_save(request, pk):
    """Сохранить или снять следующее действие. → перерисованная карточка."""
    card = get_object_or_404(_visible_cards(request), pk=pk)

    if request.POST.get("clear"):
        card.next_action = ""
        card.next_action_at = None
        card.next_action_by = None
    else:
        when, ok = _parse_action_at(request.POST.get("next_action_at"))
        if not ok:
            return render(request, "callcenter/partials/card_action_modal.html", {
                "card": card, "presets": ACTION_PRESETS,
                "suggestions": ACTION_SUGGESTIONS,
                "error": "Не разобрал дату и время — выберите их заново.",
            }, status=400)
        text = (request.POST.get("next_action") or "").strip()[:255]
        if not text and when is None:
            return render(request, "callcenter/partials/card_action_modal.html", {
                "card": card, "presets": ACTION_PRESETS,
                "suggestions": ACTION_SUGGESTIONS,
                "error": "Заполните хотя бы суть действия или срок.",
            }, status=400)
        card.next_action = text
        card.next_action_at = when
        card.next_action_by = get_employee(request.user)

    card.save(update_fields=["next_action", "next_action_at", "next_action_by",
                             "updated_at"])
    return render(request, "callcenter/partials/_card.html", {
        "card": card, "client": card.client, "now": timezone.now(),
    })


@login_required
@require_callcenter
@require_POST
def card_take(request, pk):
    """«Взять в работу»: карточка закрепляется за текущим сотрудником.

    Чужую карточку перехватывает только руководство — иначе операторы
    молча растаскивали бы работу друг у друга.
    """
    card = get_object_or_404(_visible_cards(request), pk=pk)
    me = get_employee(request.user)
    if me is None:
        return HttpResponse(status=403)
    if card.operator_id and card.operator_id != me.pk and not is_management(request.user):
        return HttpResponse(
            '<div class="alert alert-error text-sm">Карточка уже в работе '
            'у другого оператора.</div>', status=409)
    card.operator = me
    card.taken_at = timezone.now()
    card.save(update_fields=["operator", "taken_at", "updated_at"])
    return render(request, "callcenter/partials/_card.html", {
        "card": card, "client": card.client, "now": timezone.now(),
    })


@login_required
@require_callcenter
@require_POST
def card_release(request, pk):
    """Вернуть карточку в общий пул. Свою — любой, чужую — только руководство."""
    card = get_object_or_404(_visible_cards(request), pk=pk)
    me = get_employee(request.user)
    if card.operator_id and card.operator_id != (me.pk if me else None) \
            and not is_management(request.user):
        return HttpResponse(status=403)
    card.operator = None
    card.taken_at = None
    card.save(update_fields=["operator", "taken_at", "updated_at"])
    return render(request, "callcenter/partials/_card.html", {
        "card": card, "client": card.client, "now": timezone.now(),
    })


@login_required
@require_callcenter
@require_POST
def card_spam(request, pk):
    """«Спам»: номера клиента — в чёрный список, карточка — с доски.

    Автозаведённого звонком клиента, за которым ничего нет (не
    идентифицирован, без услуг и переписки), переводим в статус
    «На удаление» — существующий статус CRM ровно для такого мусора.
    Идентифицированного или уже обросшего работой клиента не трогаем:
    номер бывает общим (офис, родственник), и чёрный список номера —
    не приговор карточке клиента.
    """
    from .intake import block_phone

    card = get_object_or_404(
        CallCenterCard.objects.select_related("client").filter(
            client__in=Client.objects.visible_to(request.user)),
        pk=pk,
    )
    client = card.client
    employee = get_employee(request.user)

    phones = [p.phone for p in client.phones.all()] or [client.phone or ""]
    blocked = 0
    for phone in phones:
        obj, _ = block_phone(
            phone, comment=f"Спам с доски колл-центра · {client}"[:255],
            employee=employee,
        )
        if obj is not None:
            blocked += 1

    from_call = card.source == CallCenterCard.SOURCE_CALL
    card.delete()

    if (from_call and not client.is_identified
            and not client.services.exists() and not client.messages.exists()):
        Client.objects.filter(pk=client.pk).update(status="to_delete")

    return HttpResponse(headers={"HX-Trigger": "callcenterRefresh"})


@login_required
@require_callcenter
@require_POST
def card_remove(request, pk):
    """Снимает карточку с доски (клиент и его данные не трогаются)."""
    get_object_or_404(
        CallCenterCard.objects.filter(
            client__in=Client.objects.visible_to(request.user)),
        pk=pk,
    ).delete()
    return HttpResponse(headers={"HX-Trigger": "callcenterRefresh"})


# ──────────────── настройка колонок (Панель управления) ────────────────

@user_passes_test(is_admin)
def admin_columns(request):
    columns = CallCenterColumn.objects.annotate(cards_count=Count("cards"))
    columns = list(columns)
    return render(request, "callcenter/partials/admin_columns.html", {
        "columns": columns,
        "has_default": any(c.is_default and c.is_active for c in columns),
        # Источник без активной колонки-приёмника выключен — предупреждаем,
        # иначе «автонаполнение не работает» выглядит как поломка.
        "catch_calls": any(c.catch_unknown_calls and c.is_active for c in columns),
        "catch_leads": any(c.catch_telegram_leads and c.is_active for c in columns),
    })


@user_passes_test(is_admin)
def admin_column_edit(request, pk=None):
    col = get_object_or_404(CallCenterColumn, pk=pk) if pk else None
    if request.method == "POST":
        form = CallCenterColumnForm(request.POST, instance=col)
        if form.is_valid():
            obj = form.save(commit=False)
            if form.cleaned_data.get("order") is None:
                # Порядок не задан: новая колонка встаёт в конец, у
                # существующей остаётся прежний — админу не надо его считать.
                if col is None:
                    last = CallCenterColumn.objects.order_by("-order").first()
                    obj.order = (last.order + 10) if last else 10
                else:
                    obj.order = col.order
            obj.save()
            return HttpResponse(headers={"HX-Trigger": "reloadCallcenterColumns"})
    else:
        form = CallCenterColumnForm(instance=col)
    return render(request, "callcenter/partials/column_form_modal.html", {
        "form": form, "column": col,
    })


@user_passes_test(is_admin)
@require_POST
def admin_column_delete(request, pk):
    col = get_object_or_404(CallCenterColumn, pk=pk)
    # 🛑 on_delete=PROTECT: колонку с карточками не удаляем молча, иначе
    # карточки утащило бы каскадом вместе с работой оператора.
    if col.cards.exists():
        return HttpResponse(
            f'<div class="alert alert-error text-sm">В колонке «{col.name}» есть '
            f'карточки ({col.cards.count()}). Перенесите их на доске или '
            f'отключите колонку галочкой «Активна».</div>',
            status=409,
        )
    col.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadCallcenterColumns"})


@user_passes_test(is_admin)
@require_POST
def admin_column_move(request, pk, direction):
    """Сдвиг колонки влево/вправо — меняет order местами с соседом."""
    col = get_object_or_404(CallCenterColumn, pk=pk)
    neighbours = CallCenterColumn.objects.exclude(pk=col.pk)
    if direction == "up":
        neighbour = neighbours.filter(
            Q(order__lt=col.order) | Q(order=col.order, name__lt=col.name)
        ).order_by("-order", "-name").first()
    else:
        neighbour = neighbours.filter(
            Q(order__gt=col.order) | Q(order=col.order, name__gt=col.name)
        ).order_by("order", "name").first()
    if neighbour is not None:
        col.order, neighbour.order = neighbour.order, col.order
        if col.order == neighbour.order:
            # Порядок совпадал — разводим соседей явно, иначе обмен ничего
            # не меняет и кнопка выглядит сломанной.
            neighbour.order = col.order + (1 if direction == "up" else -1)
        CallCenterColumn.objects.filter(pk=col.pk).update(order=col.order)
        CallCenterColumn.objects.filter(pk=neighbour.pk).update(order=neighbour.order)
    return HttpResponse(headers={"HX-Trigger": "reloadCallcenterColumns"})


# ──────────────── чёрный список номеров (Панель управления) ────────────────

@user_passes_test(is_admin)
def admin_panel(request):
    """Обёртка вкладки «Колл-центр»: под-вкладки «Колонки» и «Чёрный список»."""
    return render(request, "callcenter/partials/admin_panel.html", {})


@user_passes_test(is_admin)
def admin_blacklist(request):
    q = (request.GET.get("q") or "").strip()
    qs = BlockedPhone.objects.select_related("added_by__user")
    if q:
        # Ищем по цифрам запроса: в списке номер лежит нормализованным, и
        # «+7 (900)» без этого не нашёл бы ничего.
        digits = "".join(ch for ch in q if ch.isdigit())
        qs = qs.filter(phone__contains=digits) if digits else qs.filter(comment__icontains=q)
    return render(request, "callcenter/partials/admin_blacklist.html", {
        "entries": qs[:200],
        "total": BlockedPhone.objects.filter(is_active=True).count(),
        "q": q,
        "form": BlockedPhoneForm(),
    })


@user_passes_test(is_admin)
@require_POST
def blacklist_add(request):
    from .intake import block_phone, blacklist_key

    form = BlockedPhoneForm(request.POST)
    if not form.is_valid():
        return HttpResponse(
            '<div class="alert alert-error text-sm">Проверьте номер и причину.</div>',
            status=400,
        )
    raw = form.cleaned_data["phone"]
    if not blacklist_key(raw):
        return HttpResponse(
            '<div class="alert alert-error text-sm">Не разобрал номер — '
            'введите его цифрами, например 89001234567.</div>',
            status=400,
        )
    block_phone(raw, comment=form.cleaned_data.get("comment", ""),
                employee=get_employee(request.user))
    return HttpResponse(headers={"HX-Trigger": "reloadCallcenterBlacklist"})


@user_passes_test(is_admin)
@require_POST
def blacklist_delete(request, pk):
    """Убрать номер из списка совсем — «разблокировать»."""
    get_object_or_404(BlockedPhone, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadCallcenterBlacklist"})


# ──────────────────── результат звонка (модалка у оператора) ────────────────────

def _my_outcomes(request):
    """Свои записи о звонках. Чужие не отдаём: в них номер и клиент."""
    emp = get_employee(request.user)
    if emp is None:
        return CallOutcome.objects.none()
    return CallOutcome.objects.select_related("client", "result", "employee").filter(employee=emp)


@login_required
@require_callcenter
@never_cache
def call_result_modal(request, pk):
    """Модалка «Результат звонка» — всплывает сама после разговора."""
    outcome = get_object_or_404(_my_outcomes(request), pk=pk)
    card = CallCenterCard.objects.filter(client=outcome.client).first() if outcome.client_id else None
    return render(request, "callcenter/partials/call_result_modal.html", {
        "outcome": outcome,
        "results": CallResult.objects.filter(is_active=True),
        "card": card,
        "presets": ACTION_PRESETS,
        "pending": max(_my_outcomes(request).filter(filled_at__isnull=True)
                       .exclude(pk=outcome.pk).count(), 0),
    })


@login_required
@require_callcenter
@require_POST
def call_result_save(request, pk):
    """Сохранить результат. Заодно, если заполнено, — следующее действие."""
    from apps.crm import client_log

    outcome = get_object_or_404(_my_outcomes(request), pk=pk)

    if request.POST.get("postpone"):
        # «Позже»: модалка больше не всплывает сама, но звонок остаётся
        # в счётчике незаполненных — долг не теряется.
        CallOutcome.objects.filter(pk=outcome.pk).update(postponed_at=timezone.now())
        return HttpResponse(status=204, headers={"HX-Trigger": "callcenterPendingChanged"})

    result = None
    if request.POST.get("result"):
        result = CallResult.objects.filter(pk=request.POST["result"], is_active=True).first()
    comment = (request.POST.get("comment") or "").strip()
    if result is None and not comment:
        return render(request, "callcenter/partials/call_result_modal.html", {
            "outcome": outcome,
            "results": CallResult.objects.filter(is_active=True),
            "card": CallCenterCard.objects.filter(client=outcome.client).first(),
            "presets": ACTION_PRESETS,
            "error": "Выберите результат или напишите комментарий.",
        }, status=400)

    outcome.result = result
    outcome.comment = comment
    outcome.filled_at = timezone.now()
    outcome.save(update_fields=["result", "comment", "filled_at", "updated_at"])

    # Следующее действие — по желанию, тут же, пока разговор в памяти.
    action_text = (request.POST.get("next_action") or "").strip()[:255]
    action_at, ok = _parse_action_at(request.POST.get("next_action_at"))
    if ok and (action_text or action_at) and outcome.client_id:
        card = CallCenterCard.objects.filter(client=outcome.client).first()
        if card is not None:
            card.next_action = action_text
            card.next_action_at = action_at
            card.next_action_by = get_employee(request.user)
            card.save(update_fields=["next_action", "next_action_at",
                                     "next_action_by", "updated_at"])

    # Событийка клиента: разговор — часть его истории, а не только статистика.
    if outcome.client_id:
        # 🛑 Без .capitalize(): он опускает регистр ВСЕЙ строки и портит и
        # название результата, и комментарий оператора.
        parts = [f"{outcome.get_direction_display()} звонок"]
        if result:
            parts.append(f"результат: {result.name}")
        text = " · ".join(parts) + (f"\n{comment}" if comment else "")
        client_log.record_action(
            outcome.client, "call_client",
            employee=get_employee(request.user), comment=text,
        )

    return HttpResponse(status=204, headers={
        "HX-Trigger": "callcenterRefresh, callcenterPendingChanged"})


@login_required
@require_callcenter
@never_cache
def call_result_pending(request):
    """Счётчик незаполненных результатов + ссылка на ближайший (для доски)."""
    qs = _my_outcomes(request).filter(filled_at__isnull=True).order_by("-started_at")
    return render(request, "callcenter/partials/_pending_results.html", {
        "count": qs.count(),
        "latest": qs.first(),
    })


# ──────────── справочник результатов (Панель управления) ────────────

@user_passes_test(is_admin)
def admin_results(request):
    return render(request, "callcenter/partials/admin_results.html", {
        "results": CallResult.objects.all(),
    })


@user_passes_test(is_admin)
def admin_result_edit(request, pk=None):
    obj = get_object_or_404(CallResult, pk=pk) if pk else None
    if request.method == "POST":
        form = CallResultForm(request.POST, instance=obj)
        if form.is_valid():
            item = form.save(commit=False)
            if form.cleaned_data.get("order") is None:
                last = CallResult.objects.order_by("-order").first()
                item.order = (last.order + 10) if last and obj is None else (obj.order if obj else 10)
            item.save()
            return HttpResponse(headers={"HX-Trigger": "reloadCallcenterResults"})
    else:
        form = CallResultForm(instance=obj)
    return render(request, "callcenter/partials/result_form_modal.html", {
        "form": form, "result": obj,
    })


@user_passes_test(is_admin)
@require_POST
def admin_result_delete(request, pk):
    obj = get_object_or_404(CallResult, pk=pk)
    # 🛑 on_delete=PROTECT: результат, которым уже помечены звонки, не сносим —
    # иначе история разговоров осталась бы без расшифровки.
    if obj.outcomes.exists():
        return HttpResponse(
            f'<div class="alert alert-error text-sm">Результатом «{obj.name}» '
            f'помечено звонков: {obj.outcomes.count()}. Снимите галочку '
            f'«Активен» — он исчезнет из модалки, а история сохранится.</div>',
            status=409,
        )
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadCallcenterResults"})
