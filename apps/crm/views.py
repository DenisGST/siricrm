from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string

from apps.crm.models import *
from django.db import models, transaction
from django.db.models import Q, Prefetch
from django.utils import timezone

from .models import (
    Client, Message, Address, LegalEntity, ClientEmployee,
    Service, ServiceName, PaymentProcedure, ServiceCommonStatus,
    ServiceEmployeeStatus, ServiceTag, ServiceEmployeeState,
    ServiceTagAssignment, ServiceLog, ClientEvent,
)
from .forms import ClientForm, LegalEntityForm, ServiceForm
from apps.core.models import Employee, EmployeeLog, Department
from apps.telegram.telegram_sender import create_message_and_store_file
from .tasks import send_telegram_message_task
from apps.maxchat.tasks import send_max_message_task


CLIENTS_PER_PAGE = 20
MESSAGES_PER_PAGE = 50  # сколько сообщений за раз подгружаем в телеграм


@login_required
@require_POST
def telegram_send_message(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    content = (request.POST.get("content") or "").strip()
    up_file = request.FILES.get("file")
    reply_to_id = request.POST.get("reply_to_id")
    reply_to_msg = None
    if reply_to_id:
        try:
            reply_to_msg = Message.objects.get(id=reply_to_id)
        except Message.DoesNotExist:
            pass

    if not content and not up_file:
        return HttpResponseBadRequest("Empty message")

    try:
        employee = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        employee = None

    client.last_message_at = timezone.now()
    client.save(update_fields=["last_message_at"])

    msg = create_message_and_store_file(
        client=client,
        text=content or None,
        file=up_file,
        employee=employee,
    )

    if reply_to_msg:
        msg.reply_to = reply_to_msg
        msg.save(update_fields=['reply_to'])

    send_telegram_message_task.delay(str(msg.id))

    if employee:
        ClientEmployee.objects.update_or_create(
            client=client, employee=employee,
            defaults={"messenger_status": "waiting", "status_changed_at": timezone.now()},
        )
        from apps.realtime.utils import push_messenger_status_update
        push_messenger_status_update(client)

    html = render_to_string(
        "crm/partials/telegram_message.html",
        {"msg": msg},
        request=request,
    )
    resp = HttpResponse(html)
    resp["HX-Trigger"] = "messengerStatusChanged"
    return resp


@login_required
@require_POST
def max_send_message(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    content = (request.POST.get("content") or "").strip()
    up_file = request.FILES.get("file")
    reply_to_id = request.POST.get("reply_to_id")
    reply_to_msg = None
    if reply_to_id:
        try:
            reply_to_msg = Message.objects.get(id=reply_to_id)
        except Message.DoesNotExist:
            pass

    if not content and not up_file:
        return HttpResponseBadRequest("Empty message")

    if not client.max_chat_id:
        return HttpResponseBadRequest("Client has no max_chat_id")

    try:
        employee = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        employee = None

    client.last_message_at = timezone.now()
    client.save(update_fields=["last_message_at"])

    if up_file:
        msg = create_message_and_store_file(
            client=client,
            text=content or None,
            file=up_file,
            employee=employee,
        )
        msg.channel = "max"
        msg.telegram_date = timezone.now()
        msg.reply_to = reply_to_msg
        msg.save(update_fields=["channel", "telegram_date", "reply_to"])
    else:
        msg = Message.objects.create(
            client=client,
            employee=employee,
            content=content,
            direction="outgoing",
            channel="max",
            message_type="text",
            telegram_date=timezone.now(),
            is_sent=False,
            reply_to=reply_to_msg,
        )

    send_max_message_task.delay(str(msg.id))

    if employee:
        ClientEmployee.objects.update_or_create(
            client=client, employee=employee,
            defaults={"messenger_status": "waiting", "status_changed_at": timezone.now()},
        )
        from apps.realtime.utils import push_messenger_status_update
        push_messenger_status_update(client)

    html = render_to_string(
        "crm/partials/telegram_message.html",
        {"msg": msg},
        request=request,
    )
    resp = HttpResponse(html)
    resp["HX-Trigger"] = "messengerStatusChanged"
    return resp


@login_required
def telegram_chat_for_client(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    search_q = (request.GET.get("q") or "").strip()

    qs = (
        Message.objects.filter(client=client)
        .select_related("employee", "employee__user", "file", "reply_to", "reply_to__client", "reply_to__file")
        .order_by("telegram_date", "id")
    )

    if search_q:
        qs = qs.filter(content__icontains=search_q)

    paginator = Paginator(qs, MESSAGES_PER_PAGE)
    page_param = request.GET.get("page")
    page_number = int(page_param) if page_param else (paginator.num_pages or 1)
    page_obj = paginator.get_page(page_number)

    # HX-запрос с page= → HTML фрагмент старых сообщений
    if request.headers.get("HX-Request") and page_param:
        return render(
            request,
            "crm/partials/telegram_messages_list.html",
            {"messages": page_obj.object_list},
        )

    # Статус мессенджера для текущего сотрудника
    messenger_status = "closed"
    try:
        emp = Employee.objects.get(user=request.user)
        ce = ClientEmployee.objects.filter(client=client, employee=emp).first()
        if ce:
            messenger_status = ce.messenger_status
    except Employee.DoesNotExist:
        pass

    return render(
        request,
        "crm/partials/telegram_chat_panel.html",
        {"client": client, "page_obj": page_obj, "messages": page_obj.object_list,
         "search_q": search_q, "messenger_status": messenger_status},
    )


def _telegram_clients_base_qs(emp, scope):
    """Базовый queryset клиентов для scope ('mine'/'dept'/'all'). Без search/sort/paginate.

    Используется и для самого списка, и для подсчёта количества клиентов
    в кнопках-фильтрах сверху панели.
    """
    qs = Client.objects.all()
    if emp:
        if scope == "mine":
            qs = qs.filter(employees=emp).distinct()
        elif scope == "dept" and emp.department_id:
            qs = qs.filter(employees__department_id=emp.department_id).distinct()
        # "all" → без фильтрации
    elif scope != "all":
        # Если не сотрудник и фильтр не "all" — пусто
        qs = qs.none()
    return qs


@login_required
def telegram_clients_list(request):
    page_number = request.GET.get("page") or 1
    query = (request.GET.get("q") or "").strip()
    scope = request.GET.get("scope") or "mine"
    if scope not in ("all", "dept", "mine"):
        scope = "mine"

    try:
        emp = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        emp = None

    qs = _telegram_clients_base_qs(emp, scope)

    ALLOWED_SORTS = {
        "-last_message_at", "last_message_at",
        "last_name", "-last_name",
        "first_name", "-first_name",
        "-created_at", "created_at",
    }
    sort = request.GET.get("sort") or "-last_message_at"
    if sort not in ALLOWED_SORTS:
        sort = "-last_message_at"
    # Стабильная вторичная сортировка — чтобы клиенты с одинаковым значением
    # не "прыгали" при пагинации
    qs = qs.order_by(sort, "id")

    search_q = None
    if query:
        search_q = (
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(username__icontains=query)
            | Q(phone__icontains=query)
        )
        qs = qs.filter(search_q)

    paginator = Paginator(qs, CLIENTS_PER_PAGE)
    page_obj = paginator.get_page(page_number)

    # Статусы мессенджера для текущего сотрудника
    if emp:
        statuses = dict(
            ClientEmployee.objects.filter(
                employee=emp, client__in=page_obj.object_list,
            ).values_list("client_id", "messenger_status")
        )
        for c in page_obj.object_list:
            c.ms_status = statuses.get(c.pk, "")
    else:
        for c in page_obj.object_list:
            c.ms_status = ""

    # Счётчики для кнопок-фильтров. Учитывают текущий search, чтобы цифры
    # синхронно менялись при наборе в поиске. Считаются только для page=1 —
    # на подгрузке следующих страниц цифры не меняются.
    counts = None
    if page_obj.number == 1:
        def _count(s):
            base = _telegram_clients_base_qs(emp, s)
            if search_q is not None:
                base = base.filter(search_q)
            return base.count()

        counts = {"mine": _count("mine"), "dept": _count("dept"), "all": _count("all")}

    context = {
        "page_obj": page_obj,
        "has_next": page_obj.has_next(),
        "next_page_number": page_obj.next_page_number() if page_obj.has_next() else None,
        "scope": scope,
        "query": query,
        "sort": sort,
        "counts": counts,
    }

    return render(request, "crm/partials/telegram_clients_list.html", context)



@login_required
def chat(request, client_id):
    client = get_object_or_404(
        Client.objects.prefetch_related(
            "employees",  # M2M поле
            Prefetch(
                "messages",
                queryset=Message.objects.select_related("employee").order_by(
                    "telegram_date", "id"
                ),
            ),
        ),
        id=client_id,
    )

    messages = client.messages.all()

    return render(
        request,
        "crm/clients/chat.html",
        {
            "client": client,
            "messages": messages,
        },
    )


@login_required
def dashboard(request):
    from apps.crm.bot_status import get_bot_status

    telegram_user = getattr(request.user, "telegram_user", None)

    return render(
        request,
        "dashboard.html",
        {
            "telegram_user": telegram_user,
            "telegram_bot_username": getattr(
                settings, "TELEGRAM_BOT_USERNAME", ""
            ),
            "bot_status": get_bot_status(),
            "employees_all": Employee.objects.select_related("user").order_by(
                "user__last_name", "user__first_name"
            ),
        },
    )


def _annotate_ms_status(clients, user):
    """Проставляет ms_status на объекты клиентов для текущего пользователя."""
    try:
        emp = Employee.objects.get(user=user)
        statuses = dict(
            ClientEmployee.objects.filter(
                employee=emp, client__in=clients,
            ).values_list("client_id", "messenger_status")
        )
        for c in clients:
            c.ms_status = statuses.get(c.pk, "")
    except Employee.DoesNotExist:
        for c in clients:
            c.ms_status = ""
    return clients


@login_required
def kanban(request):
    # фильтры из querystring
    employee_id         = request.GET.get("employee") or ""
    service_employee_id = request.GET.get("service_employee") or ""
    ms_status    = request.GET.get("ms_status") or ""
    created_from = request.GET.get("created_from") or ""
    created_to   = request.GET.get("created_to") or ""

    base_qs = Client.objects.all()
    if employee_id == "__none__":
        base_qs = base_qs.filter(employees__isnull=True)
    elif employee_id:
        base_qs = base_qs.filter(employees__id=employee_id)
    if service_employee_id:
        base_qs = base_qs.filter(services__employees__id=service_employee_id).distinct()
    if created_from:
        base_qs = base_qs.filter(created_at__gte=created_from)
    if created_to:
        base_qs = base_qs.filter(created_at__lte=created_to)

    if ms_status:
        try:
            emp = Employee.objects.get(user=request.user)
            ce_ids = ClientEmployee.objects.filter(
                employee=emp, messenger_status=ms_status,
            ).values_list("client_id", flat=True)
            base_qs = base_qs.filter(id__in=list(ce_ids))
        except Employee.DoesNotExist:
            base_qs = base_qs.none()

    _pfetch = ["employees", "services__name"]
    unknowns = list(base_qs.filter(status="unknown").prefetch_related(*_pfetch))
    leads = list(base_qs.filter(status="lead").prefetch_related(*_pfetch))
    actives = list(base_qs.filter(status="active").prefetch_related(*_pfetch))
    closeds = list(base_qs.filter(status="closed").prefetch_related(*_pfetch))
    archives = list(base_qs.filter(status="archive").prefetch_related(*_pfetch))

    for group in (unknowns, leads, actives, closeds, archives):
        _annotate_ms_status(group, request.user)

    employees_all = Employee.objects.filter(is_active=True).select_related("user").order_by("user__last_name", "user__first_name")
    context = {
        "unknowns": unknowns, "leads": leads, "actives": actives,
        "closeds": closeds, "archives": archives,
        "filter_employee":         employee_id,
        "filter_service_employee": service_employee_id,
        "filter_ms_status":        ms_status,
        "filter_created_from":     created_from,
        "filter_created_to":       created_to,
        "employees_all":           employees_all,
    }
    template = "crm/kanban.html"
    if request.headers.get("HX-Request"):
        template = "crm/kanban.html"
    return render(request, template, context)


@login_required
def kanban_column(request, status):
    employee_id         = request.GET.get("employee") or ""
    service_employee_id = request.GET.get("service_employee") or ""
    ms_status    = request.GET.get("ms_status") or ""
    created_from = request.GET.get("created_from") or ""
    created_to   = request.GET.get("created_to") or ""

    qs = Client.objects.filter(status=status)
    if employee_id == "__none__":
        qs = qs.filter(employees__isnull=True)
    elif employee_id:
        qs = qs.filter(employees__id=employee_id)
    if service_employee_id:
        qs = qs.filter(services__employees__id=service_employee_id).distinct()
    if created_from:
        qs = qs.filter(created_at__gte=created_from)
    if created_to:
        qs = qs.filter(created_at__lte=created_to)
    if ms_status:
        try:
            emp = Employee.objects.get(user=request.user)
            ce_ids = ClientEmployee.objects.filter(
                employee=emp, messenger_status=ms_status,
            ).values_list("client_id", flat=True)
            qs = qs.filter(id__in=list(ce_ids))
        except Employee.DoesNotExist:
            qs = qs.none()

    clients = list(qs.prefetch_related("employees", "services__name").order_by("-last_message_at"))
    _annotate_ms_status(clients, request.user)

    PAGE_SIZE = 25
    total     = len(clients)
    show_all  = request.GET.get("all") == "1"
    shown     = clients if show_all else clients[:PAGE_SIZE]
    has_more  = not show_all and total > PAGE_SIZE

    return render(request, "crm/partials/kanban_column.html", {
        "clients":   shown,
        "status":    status,
        "count":     total,
        "has_more":  has_more,
        "more_count": total - PAGE_SIZE if has_more else 0,
    })


@login_required
def client_create(request):
    if request.method == "POST":
        form = ClientForm(request.POST)
        if form.is_valid():
            client = form.save()

            if request.headers.get("HX-Request"):
                # Если запрос из таба "Адреса" — открыть edit-модалку с адресной секцией
                if request.POST.get("open_addresses"):
                    edit_form = ClientForm(instance=client)
                    return render(request, "crm/partials/client_edit_modal.html", {
                        "form": edit_form,
                        "client": client,
                        "dadata_api_key": settings.DADATA_API_KEY,
                        "open_tab": "addresses",
                    })
                return HttpResponse(
                    status=204,
                    headers={"HX-Trigger": "clientCreated"},
                )

            return redirect("clients_list")

        # Ошибки валидации — перерендерим модалку
        if request.headers.get("HX-Request"):
            return render(request, "crm/partials/client_create_modal.html", {"form": form})
        return render(request, "crm/clients/form.html", {"form": form})

    form = ClientForm()

    if request.headers.get("HX-Request"):
        return render(request, "crm/partials/client_create_modal.html", {"form": form})
    return render(request, "crm/clients/form.html", {"form": form})


@login_required
def employees_list(request):
    from django.db.models import Count, Q as DQ
    search = request.GET.get("search", "").strip()
    # partial=1 ставит поисковый input → возвращаем только результаты
    is_partial = request.GET.get("partial") == "1"

    # Базовый queryset со статистикой
    emp_qs = (
        Employee.objects.select_related("user", "department")
        .annotate(
            messages_sent=Count(
                "sent_messages",
                filter=DQ(sent_messages__direction="outgoing"),
                distinct=True,
            ),
            files_sent=Count(
                "sent_messages",
                filter=DQ(
                    sent_messages__direction="outgoing",
                    sent_messages__file__isnull=False,
                ),
                distinct=True,
            ),
        )
        .order_by("user__last_name", "user__first_name")
    )

    if search:
        emp_qs = emp_qs.filter(
            models.Q(user__first_name__icontains=search)
            | models.Q(user__last_name__icontains=search)
            | models.Q(department__name__icontains=search)
        )
        departments_with_employees = None
        no_dept_employees = None
    else:
        departments_with_employees = (
            Department.objects.prefetch_related(
                Prefetch("employees", queryset=emp_qs)
            )
            .filter(is_active=True)
            .order_by("name")
        )
        no_dept_employees = emp_qs.filter(department__isnull=True)
        emp_qs = None  # не нужен при отсутствии поиска

    ctx = {
        "departments_with_employees": departments_with_employees,
        "no_dept_employees": no_dept_employees,
        "search_employees": emp_qs,
        "search": search,
    }

    if request.headers.get("HX-Request") and is_partial:
        return render(request, "crm/employees/list_results.html", ctx)
    if request.headers.get("HX-Request"):
        return render(request, "crm/employees/list_partial.html", ctx)
    return render(request, "crm/employees/list.html", ctx)


@login_required
def logs_list(request):
    search = request.GET.get("search", "").strip()
    qs = EmployeeLog.objects.select_related("employee", "client", "message")

    if search:
        qs = qs.filter(
            models.Q(employee__user__first_name__icontains=search)
            | models.Q(employee__user__last_name__icontains=search)
            | models.Q(action__icontains=search)
            | models.Q(description__icontains=search)
            | models.Q(client__first_name__icontains=search)
            | models.Q(client__last_name__icontains=search)
        )

    paginator = Paginator(qs.order_by("-timestamp"), 50)
    page_number = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_number)

    template = (
        "crm/logs/list_partial.html"
        if request.headers.get("HX-Request")
        else "crm/logs/list.html"
    )

    return render(
        request,
        template,
        {
            "page_obj": page_obj,
            "search": search,
        },
    )


@login_required
def clients_list(request):
    search = request.GET.get("search", "").strip()

    qs = Client.objects.prefetch_related("employees")

    if search:
        qs = qs.filter(
            models.Q(first_name__icontains=search)
            | models.Q(last_name__icontains=search)
            | models.Q(username__icontains=search)
            | models.Q(phone__icontains=search)
            | models.Q(email__icontains=search)
        )

    paginator = Paginator(qs.order_by("-last_message_at"), 15)
    page_number = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_number)

    # partial=1 -> только кусок таблицы
    if request.headers.get("HX-Request") and request.GET.get("partial") == "1":
        template = "crm/clients/list_partial.html"
    else:
        template = "crm/clients/list.html"

    return render(
        request,
        template,
        {
            "page_obj": page_obj,
            "search": search,
        },
    )


@login_required
def employees_online_count(request):
    count = Employee.objects.filter(is_online=True).count()
    return HttpResponse(count)


@login_required
def clients_active_count(request):
    count = Client.objects.filter(status="active").count()
    return HttpResponse(count)


@login_required
def messages_new_count(request):
    count = Message.objects.filter(is_read=False).count()
    return HttpResponse(count)


@login_required
def lead_count(request):
    count = Client.objects.filter(status="lead").count()
    return HttpResponse(count)


@require_POST
def telegram_import_history(request, client_id):
    from apps.crm.tasks import import_telegram_history_task

    db_client = get_object_or_404(Client, id=client_id)

    if not db_client.telegram_id:
        return JsonResponse({"error": "Нет telegram_id у клиента"}, status=400)

    task = import_telegram_history_task.delay(db_client.telegram_id, limit=300)
    return JsonResponse({"ok": True, "task_id": task.id})


@login_required
def task_status(request, task_id):
    from celery.result import AsyncResult

    result = AsyncResult(task_id)
    meta = result.info if isinstance(result.info, dict) else {}
    return JsonResponse(
        {
            "status": result.status,
            "ready": result.ready(),
            "current": meta.get("current", 0),
            "total": meta.get("total", 0),
        }
    )

@login_required
def client_edit(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    if request.method == "POST":
        form = ClientForm(request.POST, instance=client)
        if form.is_valid():
            form.save()
            if request.headers.get("HX-Request"):
                return HttpResponse(
                    '<script>window.location.reload();</script>'
                )
            return redirect("chat", client_id=client_id)
        else:
            if request.headers.get("HX-Request"):
                return render(request, "crm/partials/client_edit_modal.html", {
                    "form": form, "client": client,
                    "dadata_api_key": settings.DADATA_API_KEY,
                })
    else:
        form = ClientForm(instance=client)

    return render(request, "crm/partials/client_edit_modal.html", {
        "form": form, "client": client,
        "dadata_api_key": settings.DADATA_API_KEY,
    })


@login_required
def client_merge_search(request):
    """HTMX: search clients to merge with (excludes the source client)."""
    source_id = request.GET.get("source")
    query = request.GET.get("merge_q", "").strip()

    clients = Client.objects.none()
    if query and len(query) >= 2:
        clients = (
            Client.objects
            .filter(
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(patronymic__icontains=query)
                | Q(phone__icontains=query)
                | Q(username__icontains=query)
            )
            .exclude(pk=source_id)
            .order_by("last_name", "first_name")[:20]
        )

    return render(request, "crm/partials/client_merge_results.html", {
        "clients": clients,
        "source_id": source_id,
        "query": query,
    })


@login_required
@require_POST
def message_react(request, msg_id):
    """Поставить реакцию на сообщение (Telegram или MAX)."""
    from .tasks import send_reaction_task

    msg = get_object_or_404(Message, pk=msg_id)
    emoji = (request.POST.get("emoji") or "").strip()

    ALLOWED = {"👍", "❤️", "🔥", "🥰", "👏", "😁", "🎉", "🤔", "😢", "👎", "😮", "🤯"}
    if emoji not in ALLOWED:
        return HttpResponseBadRequest("Invalid emoji")

    if msg.channel == "telegram":
        if not msg.telegram_message_id or not msg.client.telegram_id:
            return HttpResponseBadRequest("Message is not linked to Telegram")
    elif msg.channel == "max":
        if not msg.max_message_id or not msg.client.max_chat_id:
            return HttpResponseBadRequest("Message is not linked to MAX")
    else:
        return HttpResponseBadRequest("Unknown channel")

    send_reaction_task.delay(str(msg.id), emoji)
    return HttpResponse(status=204)


@login_required
@require_POST
def client_merge(request, client_id):
    """Merge target client into source (client_id).
    All messages, services and employees from target are moved/merged into source.
    Target client is then deleted.
    """
    source = get_object_or_404(Client, pk=client_id)
    target_id = request.POST.get("target_id")
    target = get_object_or_404(Client, pk=target_id)

    with transaction.atomic():
        # Clear unique fields on target FIRST to avoid constraint violations
        if target.telegram_id:
            target_tg_id = target.telegram_id
            target.telegram_id = None
            target.save(update_fields=["telegram_id"])
            if not source.telegram_id:
                source.telegram_id = target_tg_id

        if target.max_chat_id:
            target_max_id = target.max_chat_id
            target.max_chat_id = None
            target.save(update_fields=["max_chat_id"])
            if not source.max_chat_id:
                source.max_chat_id = target_max_id

        # Fill in missing contact info from target
        for field in ("first_name", "phone", "email", "username", "last_name", "patronymic",
                      "birth_date", "birth_place", "passport_series", "passport_number",
                      "passport_issued_by", "passport_issued_date", "inn", "snils", "notes"):
            if not getattr(source, field) and getattr(target, field):
                setattr(source, field, getattr(target, field))

        source.save()

        # Move messages
        Message.objects.filter(client=target).update(client=source)

        # Move services
        Service.objects.filter(client=target).update(client=source)

        # Merge employees M2M
        for ce in target.client_employees.all():
            ClientEmployee.objects.get_or_create(
                client=source, employee=ce.employee,
                defaults={"messenger_status": ce.messenger_status},
            )

        # Delete duplicate
        target.delete()

    # Re-render the chat panel with the updated source client
    qs = (
        Message.objects
        .filter(client=source)
        .select_related("employee", "employee__user", "file", "reply_to", "reply_to__client")
        .order_by("telegram_date", "id")
    )
    paginator = Paginator(qs, MESSAGES_PER_PAGE)
    page_number = paginator.num_pages or 1
    page_obj = paginator.get_page(page_number)

    return render(request, "crm/partials/telegram_chat_panel.html", {
        "client": source,
        "messages": page_obj.object_list,
        "page_obj": page_obj,
        "merge_success": True,
    })


# ─── Адреса клиента ───

DADATA_ADDRESS_FIELDS = [
    "postal_code", "country", "country_iso_code", "federal_district",
    "region_fias_id", "region_kladr_id", "region_with_type", "region_type_full", "region",
    "area_fias_id", "area_with_type", "area_type_full", "area",
    "city_fias_id", "city_kladr_id", "city_with_type", "city_type_full", "city",
    "city_district_with_type",
    "settlement_fias_id", "settlement_with_type", "settlement_type_full", "settlement",
    "street_fias_id", "street_with_type", "street_type_full", "street",
    "house_fias_id", "house_type_full", "house",
    "block_type_full", "block", "entrance", "floor",
    "flat_type_full", "flat",
    "fias_id", "fias_level", "kladr_id",
    "geo_lat", "geo_lon",
    "qc_geo", "qc_complete", "qc_house", "qc",
    "okato", "oktmo", "tax_office", "timezone",
]


@login_required
def client_addresses(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    addresses = client.addresses.order_by("address_type")
    return render(request, "crm/partials/address_list.html", {
        "client": client, "addresses": addresses,
        "dadata_api_key": settings.DADATA_API_KEY,
    })


@login_required
def address_form(request, client_id, address_id=None):
    client = get_object_or_404(Client, pk=client_id)
    address = get_object_or_404(Address, pk=address_id, client=client) if address_id else None

    if request.method == "POST":
        if address:
            addr = address
        else:
            addr = Address(client=client)

        addr.address_type = request.POST.get("address_type", "default")
        addr.comment = request.POST.get("comment", "")
        addr.source = request.POST.get("source", "")
        addr.result = request.POST.get("result", "") or addr.source

        for field in DADATA_ADDRESS_FIELDS:
            setattr(addr, field, request.POST.get(field, ""))

        addr.save()
        addresses = client.addresses.order_by("address_type")
        return render(request, "crm/partials/address_list.html", {
            "client": client, "addresses": addresses,
            "dadata_api_key": settings.DADATA_API_KEY,
        })

    return render(request, "crm/partials/address_form.html", {
        "client": client, "address": address or Address(),
        "address_types": Address.ADDRESS_TYPES,
        "dadata_api_key": settings.DADATA_API_KEY,
        "is_new": address is None,
    })


@login_required
@require_POST
def address_delete(request, client_id, address_id):
    address = get_object_or_404(Address, pk=address_id, client_id=client_id)
    address.delete()
    client = get_object_or_404(Client, pk=client_id)
    addresses = client.addresses.order_by("address_type")
    return render(request, "crm/partials/address_list.html", {
        "client": client, "addresses": addresses,
        "dadata_api_key": settings.DADATA_API_KEY,
    })


# ─── Статус мессенджера ───

STATUS_CYCLE = {"closed": "waiting", "waiting": "open", "open": "closed"}

def _render_status_badge(status: str) -> str:
    if status == "open":
        return '<span class="badge badge-sm gap-1" style="background:#ef4444;color:#fff;border:none">Диалог открыт</span>'
    if status == "waiting":
        return '<span class="badge badge-sm gap-1" style="background:#3b82f6;color:#fff;border:none">Ожидаю ответа</span>'
    return '<span class="badge badge-sm gap-1" style="background:#22c55e;color:#fff;border:none">Диалог закрыт</span>'


@login_required
@require_POST
def cycle_dialog_status(request, client_id):
    """Циклически переключает статус: closed → waiting → open → closed."""
    client = get_object_or_404(Client, pk=client_id)
    try:
        employee = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        return HttpResponseBadRequest("No employee")

    ce, _ = ClientEmployee.objects.get_or_create(client=client, employee=employee)
    next_status = STATUS_CYCLE.get(ce.messenger_status, "closed")
    ce.messenger_status = next_status
    ce.status_changed_at = timezone.now()
    ce.save(update_fields=["messenger_status", "status_changed_at"])

    if next_status == "closed":
        from apps.crm.event_logger import log_dialog_ended
        log_dialog_ended(client, employee=employee)

    from apps.realtime.utils import push_messenger_status_update
    push_messenger_status_update(client)

    return HttpResponse(_render_status_badge(next_status))


@login_required
def notifications_count(request):
    """Счётчик непрочитанных диалогов текущего сотрудника (messenger_status=open)."""
    count = 0
    try:
        emp = Employee.objects.get(user=request.user)
        count = ClientEmployee.objects.filter(
            employee=emp, messenger_status="open"
        ).count()
    except Employee.DoesNotExist:
        pass
    return render(request, "crm/partials/notif_count.html", {"count": count})


@login_required
def global_search(request):
    """Глобальный поиск: клиенты, юр.лица, сообщения. Возвращает HTML-дропдаун."""
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return render(request, "crm/partials/global_search_results.html",
                      {"q": q, "clients": [], "legal_entities": [], "messages": [], "empty": True})

    clients = Client.objects.filter(
        Q(first_name__icontains=q) | Q(last_name__icontains=q) |
        Q(patronymic__icontains=q) | Q(username__icontains=q) |
        Q(phone__icontains=q)
    ).order_by("last_name", "first_name")[:12]

    legal_entities = LegalEntity.objects.filter(
        Q(name__icontains=q) | Q(inn__icontains=q) | Q(ogrn__icontains=q)
    ).order_by("name")[:12]

    messages = Message.objects.filter(
        content__icontains=q
    ).select_related("client").order_by("-created_at")[:12]

    empty = not (clients or legal_entities or messages)
    return render(request, "crm/partials/global_search_results.html", {
        "q": q, "clients": clients, "legal_entities": legal_entities,
        "messages": messages, "empty": empty,
    })


@login_required
def messenger_status_badge(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    status = "closed"
    try:
        emp = Employee.objects.get(user=request.user)
        ce = ClientEmployee.objects.filter(client=client, employee=emp).first()
        if ce:
            status = ce.messenger_status
    except Employee.DoesNotExist:
        pass
    return HttpResponse(_render_status_badge(status))


# ─── Юридические лица ───

@login_required
def legal_entities_list(request):
    from apps.crm.models import LegalEntityKind, Region

    search = request.GET.get("search", "").strip()
    f_kind = request.GET.get("kind", "").strip()
    f_status = request.GET.get("status", "").strip()
    f_entity_type = request.GET.get("entity_type", "").strip()
    f_region = request.GET.get("region", "").strip()
    sort = (request.GET.get("sort") or "name").strip()

    ALLOWED_SORT = {
        "name", "kind__short_name", "entity_type", "inn", "ogrn",
        "director_name", "status", "brand", "region__name",
    }
    sort_key = sort.lstrip("-")
    if sort_key not in ALLOWED_SORT:
        sort = "name"

    qs = LegalEntity.objects.select_related("kind", "region").all()

    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(short_name__icontains=search)
            | Q(brand__icontains=search)
            | Q(inn__icontains=search)
            | Q(ogrn__icontains=search)
        )
    if f_kind:
        qs = qs.filter(kind_id=f_kind)
    if f_status:
        qs = qs.filter(status=f_status)
    if f_entity_type:
        qs = qs.filter(entity_type=f_entity_type)
    if f_region:
        if f_region == "none":
            qs = qs.filter(region__isnull=True)
        else:
            qs = qs.filter(region_id=f_region)

    paginator = Paginator(qs.order_by(sort, "name"), 20)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    if request.headers.get("HX-Request") and request.GET.get("partial") == "1":
        template = "crm/legal_entities/list_partial.html"
    else:
        template = "crm/legal_entities/list.html"

    return render(request, template, {
        "page_obj": page_obj,
        "search": search,
        "f_kind": f_kind,
        "f_status": f_status,
        "f_entity_type": f_entity_type,
        "f_region": f_region,
        "sort": sort,
        "kinds": LegalEntityKind.objects.order_by("name"),
        "regions": Region.objects.order_by("number"),
        "status_choices": LegalEntity.STATUS_CHOICES,
        "entity_type_choices": LegalEntity.ENTITY_TYPE_CHOICES,
    })


@login_required
def legal_entity_create(request):
    if request.method == "POST":
        form = LegalEntityForm(request.POST)
        if form.is_valid():
            form.save()
            if request.headers.get("HX-Request"):
                # Пустое тело + outerHTML-swap → модалка удаляется из DOM.
                # HX-Trigger обновит список юр.лиц.
                resp = HttpResponse("")
                resp["HX-Trigger"] = "legalEntityChanged"
                return resp
            return redirect("legal_entities_list")
        # Форма невалидна — показываем её снова с ошибками
    else:
        form = LegalEntityForm()

    return render(request, "crm/legal_entities/form_modal.html", {
        "form": form, "legal_entity": LegalEntity(), "is_new": True,
        "dadata_api_key": settings.DADATA_API_KEY,
    })


@login_required
def legal_entity_edit(request, le_id):
    le = get_object_or_404(LegalEntity, pk=le_id)
    if request.method == "POST":
        form = LegalEntityForm(request.POST, instance=le)
        if form.is_valid():
            form.save()
            if request.headers.get("HX-Request"):
                resp = HttpResponse("")
                resp["HX-Trigger"] = "legalEntityChanged"
                return resp
            return redirect("legal_entities_list")
    else:
        form = LegalEntityForm(instance=le)

    return render(request, "crm/legal_entities/form_modal.html", {
        "form": form, "legal_entity": le,
        "dadata_api_key": settings.DADATA_API_KEY,
    })


@login_required
def legal_entity_detail(request, le_id):
    le = get_object_or_404(LegalEntity, pk=le_id)
    return render(request, "crm/legal_entities/detail_modal.html", {"le": le})


# ─────────────────────────────────────────────
# Услуги (Service)
# ─────────────────────────────────────────────

def _current_employee(request):
    try:
        return request.user.employee
    except Employee.DoesNotExist:
        return None


def _visible_services_qs(user):
    """Услуги, к которым у пользователя есть доступ по services_allowed."""
    qs = Service.objects.select_related(
        "client", "agent", "name", "region", "common_status", "payment_procedure",
    ).prefetch_related(
        "employee_states__employee__user", "employee_states__status",
        "tag_assignments__tag", "tag_assignments__employee",
    )
    if user.is_superuser:
        return qs
    emp = _current_employee_from_user(user)
    if not emp:
        return qs.none()
    # admin/head_dep видят всё из своих отделов / всех; обычные — только где есть доступ к этой услуге
    if emp.role in ("admin", "head_dep"):
        return qs
    allowed_ids = list(emp.services_allowed.values_list("id", flat=True))
    return qs.filter(name_id__in=allowed_ids)


def _current_employee_from_user(user):
    try:
        return user.employee
    except Employee.DoesNotExist:
        return None


@login_required
def services_list(request):
    qs = _visible_services_qs(request.user).order_by("-created_at")
    return render(request, "crm/services/list.html", {"services": qs[:200]})


@login_required
def service_edit(request, pk=None):
    emp = _current_employee_from_user(request.user)
    svc = get_object_or_404(Service, pk=pk) if pk else None

    # Контроль доступа: нельзя редактировать услугу, к которой нет доступа.
    if svc and emp and not request.user.is_superuser and emp.role not in ("admin", "head_dep"):
        if not emp.services_allowed.filter(pk=svc.name_id).exists():
            return HttpResponse("Нет доступа к услуге", status=403)

    preset_client_id = request.GET.get("client") or request.POST.get("_preset_client")
    preset_client = Client.objects.filter(pk=preset_client_id).first() if preset_client_id else None

    from apps.crm.models import ServiceEmployeeState, ServiceEmployeeStatus
    import json
    emp_state     = ServiceEmployeeState.objects.filter(service=svc, employee=emp).first() if svc and emp else None
    emp_statuses  = ServiceEmployeeStatus.objects.filter(employee=emp, is_active=True).select_related(
        "common_status"
    ).order_by("common_status__order", "order", "name") if emp else ServiceEmployeeStatus.objects.none()

    emp_statuses_by_common = {}
    for s in emp_statuses:
        key = str(s.common_status_id)
        emp_statuses_by_common.setdefault(key, []).append({"id": str(s.id), "name": s.name})
    emp_statuses_json = json.dumps(emp_statuses_by_common)

    if request.method == "POST":
        form = ServiceForm(request.POST, request.FILES, instance=svc, current_employee=emp)
        if form.is_valid():
            is_new = svc is None
            svc_new = form.save(commit=False)
            uploaded = request.FILES.get("contract_file_upload")
            if uploaded:
                from apps.files.s3_utils import upload_file_to_s3
                from apps.files.models import StoredFile
                bucket, key = upload_file_to_s3(
                    uploaded.read(), prefix="contracts",
                    filename=uploaded.name, content_type=uploaded.content_type,
                )
                sf = StoredFile.objects.create(
                    bucket=bucket, key=key, filename=uploaded.name,
                    content_type=uploaded.content_type or "",
                    size=uploaded.size,
                )
                svc_new.contract_file = sf
            svc_new.save()
            form.save_m2m()

            if is_new and emp:
                assigned = []

                # Создатель услуги
                ServiceEmployeeState.objects.get_or_create(service=svc_new, employee=emp)
                assigned.append(emp)

                # Руководитель отдела (если есть и отличается от создателя)
                if emp.department_id:
                    dept = emp.department
                    if dept.manager_id:
                        try:
                            head = Employee.objects.get(user_id=dept.manager_id)
                            if head != emp:
                                ServiceEmployeeState.objects.get_or_create(
                                    service=svc_new, employee=head
                                )
                                assigned.append(head)
                        except Employee.DoesNotExist:
                            pass

                # Лог клиента
                if svc_new.client_id:
                    svc_label = svc_new.numb_dogovor or svc_new.name.short_name
                    ClientEvent.objects.create(
                        client_id=svc_new.client_id,
                        event_type="service_created",
                        description=f"Добавлена услуга: {svc_label}",
                        new_value=svc_new.name.short_name,
                        employee=emp,
                    )
                    if assigned:
                        names = ", ".join(a.user.get_full_name() or a.user.username for a in assigned)
                        ClientEvent.objects.create(
                            client_id=svc_new.client_id,
                            event_type="employee_assigned",
                            description=f"Услуга {svc_label}: назначены исполнители — {names}",
                            employee=emp,
                        )

            # Сохраняем личный статус сотрудника
            emp_status_id = request.POST.get("emp_status")
            if emp and svc_new.pk:
                state, _ = ServiceEmployeeState.objects.get_or_create(
                    service=svc_new, employee=emp,
                )
                new_status = ServiceEmployeeStatus.objects.filter(pk=emp_status_id, employee=emp).first() if emp_status_id else None
                if state.status != new_status:
                    state.status     = new_status
                    state.updated_by = emp
                    state.save(update_fields=["status", "updated_by", "updated_at"])

            resp = HttpResponse("")
            resp["HX-Trigger"] = '{"serviceChanged": "", "kanbanRefresh": ""}'
            return resp
    else:
        initial = {"client": preset_client.pk} if preset_client else {}
        form = ServiceForm(instance=svc, current_employee=emp, initial=initial)

    return render(request, "crm/services/form_modal.html", {
        "form": form, "service": svc, "preset_client": preset_client,
        "emp_state": emp_state, "emp_statuses": emp_statuses,
        "emp_statuses_json": emp_statuses_json,
    })


@login_required
@require_POST
def service_delete(request, pk):
    emp = _current_employee_from_user(request.user)
    svc = get_object_or_404(Service, pk=pk)
    if not request.user.is_superuser and (not emp or emp.role not in ("admin", "head_dep")):
        return HttpResponse("Нет доступа", status=403)
    client_id  = svc.client_id
    svc_label  = svc.numb_dogovor or svc.name.short_name
    svc_name   = svc.name.short_name
    svc.delete()
    if client_id:
        ClientEvent.objects.create(
            client_id=client_id,
            event_type="service_deleted",
            description=f"Удалена услуга: {svc_label}",
            old_value=svc_name,
            employee=emp,
        )
    return HttpResponse("", headers={"HX-Trigger": '{"serviceChanged": "", "kanbanRefresh": ""}'})


@login_required
def service_client_search(request):
    """HTMX-поиск клиента/агента для формы услуги."""
    q = (request.GET.get("q") or "").strip()
    target = request.GET.get("target") or "client"  # client | agent
    clients = Client.objects.none()
    if len(q) >= 2:
        clients = (
            Client.objects.filter(
                Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(patronymic__icontains=q)
                | Q(phone__icontains=q)
                | Q(username__icontains=q)
            ).order_by("last_name", "first_name")[:15]
        )
    return render(request, "crm/services/client_search_results.html", {
        "clients": clients, "target": target, "query": q,
    })


# ─── Канбан по услугам (общие статусы) ───

@login_required
def services_kanban(request):
    service_name_id = request.GET.get("service_name") or ""
    employee_id     = request.GET.get("employee") or ""
    department_id   = request.GET.get("department") or ""
    service_names   = ServiceName.objects.filter(is_active=True).order_by("short_name")
    employees_all   = Employee.objects.filter(is_active=True).select_related("user").order_by(
        "user__last_name", "user__first_name"
    )
    departments_all = Department.objects.filter(is_active=True).order_by("name")
    statuses = ServiceCommonStatus.objects.filter(is_active=True)
    if service_name_id:
        statuses = statuses.filter(service_name_id=service_name_id)
    if department_id:
        statuses = statuses.filter(department_id=department_id)
    statuses = statuses.select_related("service_name", "department").order_by(
        "service_name__short_name", "order"
    )
    return render(request, "crm/kanban_services.html", {
        "statuses": statuses,
        "service_names": service_names,
        "employees_all": employees_all,
        "departments_all": departments_all,
        "filter_service_name": service_name_id,
        "filter_employee": employee_id,
        "filter_department": department_id,
    })


@login_required
def services_kanban_column(request, status_id):
    status      = get_object_or_404(ServiceCommonStatus, pk=status_id)
    employee_id = request.GET.get("employee") or ""
    qs = _visible_services_qs(request.user).filter(common_status=status)
    if employee_id:
        qs = qs.filter(employees__id=employee_id)
    qs = qs.order_by("-created_at")
    services = list(qs[:200])
    return render(request, "crm/partials/kanban_services_column.html", {
        "services": services, "status": status,
        "for_my_kanban": False, "count": len(services),
    })


@login_required
@require_POST
def service_move(request, pk):
    """Drag-and-drop: смена common_status услуги между колонками канбана."""
    service    = get_object_or_404(Service, pk=pk)
    status_id  = request.POST.get("status_id")
    new_status = get_object_or_404(ServiceCommonStatus, pk=status_id)

    if new_status.service_name_id != service.name_id:
        return HttpResponseBadRequest("Нельзя переместить в статус другой услуги")

    old_status = service.common_status
    if old_status == new_status:
        return HttpResponse(status=204)

    service.common_status = new_status
    service.save(update_fields=["common_status"])

    try:
        actor = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        actor = None

    ServiceLog.objects.create(
        service=service,
        employee=actor,
        action="common_status_change",
        old_common_status=old_status,
        new_common_status=new_status,
    )

    if service.client_id:
        svc_label  = service.numb_dogovor or service.name.short_name
        actor_name = actor.user.get_full_name() if actor else "система"
        ClientEvent.objects.create(
            client_id=service.client_id,
            event_type="status_change",
            description=(
                f"Услуга {svc_label}: статус изменён "
                f"«{old_status.name}» → «{new_status.name}»"
            ),
            old_value=old_status.name,
            new_value=new_status.name,
            employee=actor,
        )

    return HttpResponse(status=204)


# ─── Мой канбан (статусы пер-сотрудник) ───

@login_required
def my_kanban(request):
    from itertools import groupby as _groupby
    current_emp = _current_employee_from_user(request.user)
    if not current_emp:
        return HttpResponse("Нужен профиль сотрудника", status=403)

    # Права на просмотр чужих канбанов
    CAN_VIEW_OTHERS = (
        request.user.is_superuser or
        current_emp.role in ("head_dep", "managing_partner", "admin")
    )

    service_name_id  = request.GET.get("service_name") or ""
    common_status_id = request.GET.get("common_status") or ""
    viewed_emp_id    = request.GET.get("viewed_employee") or ""

    # Определяем чей канбан показываем
    if CAN_VIEW_OTHERS and viewed_emp_id:
        emp = get_object_or_404(Employee, pk=viewed_emp_id, is_active=True)
    else:
        emp = current_emp
        viewed_emp_id = ""

    emp_statuses_qs = ServiceEmployeeStatus.objects.filter(employee=emp, is_active=True)
    if service_name_id:
        emp_statuses_qs = emp_statuses_qs.filter(common_status__service_name_id=service_name_id)
    if common_status_id:
        emp_statuses_qs = emp_statuses_qs.filter(common_status_id=common_status_id)

    emp_statuses = list(
        emp_statuses_qs.select_related("common_status__service_name")
        .order_by("common_status__service_name__short_name", "common_status__order", "order")
    )

    groups = []
    for cs, statuses_iter in _groupby(emp_statuses, key=lambda s: s.common_status_id):
        statuses_list = list(statuses_iter)
        groups.append({
            "common_status": statuses_list[0].common_status,
            "statuses": statuses_list,
        })

    all_cs_ids = ServiceEmployeeStatus.objects.filter(
        employee=emp, is_active=True
    ).values_list("common_status_id", flat=True).distinct()
    common_statuses = ServiceCommonStatus.objects.filter(
        id__in=all_cs_ids
    ).select_related("service_name").order_by("service_name__short_name", "order")

    service_names = emp.services_allowed.filter(is_active=True).order_by("short_name")

    # Список сотрудников для фильтра (только для руководителей)
    employees_for_filter = []
    if CAN_VIEW_OTHERS:
        employees_for_filter = (
            Employee.objects.filter(is_active=True)
            .exclude(pk=current_emp.pk)
            .select_related("user", "department")
            .order_by("department__name", "user__last_name", "user__first_name")
        )

    return render(request, "crm/kanban_my.html", {
        "groups": groups,
        "service_names": service_names,
        "common_statuses": common_statuses,
        "filter_service_name": service_name_id,
        "filter_common_status": common_status_id,
        "can_view_others": CAN_VIEW_OTHERS,
        "employees_for_filter": employees_for_filter,
        "viewed_emp": emp if emp != current_emp else None,
        "viewed_emp_id": viewed_emp_id,
    })


@login_required
def my_kanban_column(request, status_id):
    current_emp = _current_employee_from_user(request.user)
    if not current_emp:
        return HttpResponse("", status=403)

    viewed_emp_id = request.GET.get("viewed_employee") or ""
    CAN_VIEW_OTHERS = (
        request.user.is_superuser or
        current_emp.role in ("head_dep", "managing_partner", "admin")
    )
    if CAN_VIEW_OTHERS and viewed_emp_id:
        emp = get_object_or_404(Employee, pk=viewed_emp_id, is_active=True)
    else:
        emp = current_emp

    status = get_object_or_404(ServiceEmployeeStatus, pk=status_id, employee=emp)
    service_ids = ServiceEmployeeState.objects.filter(
        employee=emp, status=status,
    ).values_list("service_id", flat=True)
    qs = (
        Service.objects.filter(id__in=list(service_ids))
        .select_related("client", "name", "region", "common_status")
        .prefetch_related(
            "employee_states__employee__user", "employee_states__status",
            "tag_assignments__tag", "tag_assignments__employee",
        )
        .order_by("-created_at")
    )
    services = list(qs[:200])
    return render(request, "crm/partials/kanban_services_column.html", {
        "services": services, "status": status,
        "for_my_kanban": True, "current_employee": emp,
        "count": len(services),
        "drag_fn_start": "myDragStart",
        "drag_fn_end": "myDragEnd",
    })


@login_required
@require_POST
def service_my_move(request, pk):
    """Drag-and-drop в Моём канбане: смена личного статуса услуги."""
    emp = _current_employee_from_user(request.user)
    if not emp:
        return HttpResponse("", status=403)

    service    = get_object_or_404(Service, pk=pk)
    status_id  = request.POST.get("status_id")
    new_status = get_object_or_404(ServiceEmployeeStatus, pk=status_id, employee=emp)

    state = ServiceEmployeeState.objects.filter(service=service, employee=emp).first()
    if not state:
        return HttpResponseBadRequest("Нет состояния для этой услуги")

    old_status = state.status
    if old_status == new_status:
        return HttpResponse(status=204)

    state.status = new_status
    state.save(update_fields=["status"])

    ServiceLog.objects.create(
        service=service,
        employee=emp,
        action="status_change",
        old_status=old_status,
        new_status=new_status,
    )

    if service.client_id:
        svc_label = service.numb_dogovor or service.name.short_name
        ClientEvent.objects.create(
            client_id=service.client_id,
            event_type="status_change",
            description=(
                f"Услуга {svc_label}: мой статус изменён "
                f"«{old_status.name if old_status else '—'}» → «{new_status.name}»"
            ),
            old_value=old_status.name if old_status else "",
            new_value=new_status.name,
            employee=emp,
        )
    return HttpResponse(status=204)


@login_required
def client_events_modal(request, client_id):
    client = get_object_or_404(Client.objects.prefetch_related("employees__user"), pk=client_id)
    events = ClientEvent.objects.filter(client=client).select_related(
        "employee__user"
    ).order_by("-created_at")
    return render(request, "crm/partials/client_events_modal.html", {
        "client": client,
        "events": events,
    })


@login_required
def client_identify_modal(request, client_id):
    """
    HTMX: модалка «Идентификация».
    GET  — открывает модалку: слева текущие ФИО/телефон из БД, справа —
           данные из Telegram (через userbot).
    POST — сохраняет правки, ставит is_identified=True и пишет
           ClientEvent('client_identified').
    """
    from apps.telegram.identify_helper import identify_get_telegram_info

    client = get_object_or_404(Client, pk=client_id)

    if request.method == "POST":
        last_name  = (request.POST.get("last_name")  or "").strip()
        first_name = (request.POST.get("first_name") or "").strip()
        patronymic = (request.POST.get("patronymic") or "").strip()
        phone      = (request.POST.get("phone")      or "").strip()

        if not first_name:
            return HttpResponseBadRequest("Имя обязательно")

        old_repr = (
            f"{client.last_name} {client.first_name} {client.patronymic}".strip()
            + (f", тел. {client.phone}" if client.phone else "")
        )

        with transaction.atomic():
            client.last_name = last_name
            client.first_name = first_name
            client.patronymic = patronymic
            client.phone = phone or None
            client.is_identified = True
            client.save(update_fields=[
                "last_name", "first_name", "patronymic", "phone",
                "is_identified", "updated_at",
            ])

            try:
                actor = Employee.objects.get(user=request.user)
            except Employee.DoesNotExist:
                actor = None

            new_repr = (
                f"{last_name} {first_name} {patronymic}".strip()
                + (f", тел. {phone}" if phone else "")
            )
            ClientEvent.objects.create(
                client=client,
                event_type="client_identified",
                description=f"Идентифицирован. Было: «{old_repr}» → стало: «{new_repr}».",
                old_value=old_repr[:255],
                new_value=new_repr[:255],
                employee=actor,
            )

        # Канбан должен перерисовать карточку (цвет ФИО + исчезновение
        # кнопки «i») и модалка — закрыться. С hx-swap="none" сам ответ
        # HTMX в DOM не вставляет, поэтому используем заголовок
        # HX-Refresh: true — он триггерит full-page reload и убирает
        # модалку вместе со страницей.
        resp = HttpResponse(status=204)
        resp["HX-Refresh"] = "true"
        return resp

    # GET — тянем данные из Telegram
    tg_info = identify_get_telegram_info(client.telegram_id) if client.telegram_id else {
        "ok": False,
        "error": "У клиента не задан telegram_id.",
    }
    return render(request, "crm/partials/client_identify_modal.html", {
        "client": client,
        "tg": tg_info,
    })


@login_required
def client_assign_employee_picker(request, client_id):
    """HTMX: возвращает попап с выбором ответственного сотрудника."""
    client = get_object_or_404(Client, pk=client_id)
    employees = Employee.objects.filter(is_active=True).select_related("user").order_by(
        "user__last_name", "user__first_name"
    )
    current_id = request.GET.get("current")
    if current_id:
        current = Employee.objects.filter(pk=current_id).first()
    else:
        current = client.employees.first()
    return render(request, "crm/partials/assign_employee_picker.html", {
        "client": client,
        "employees": employees,
        "current": current,
    })


@login_required
@require_POST
def client_assign_employee(request, client_id):
    """HTMX: назначает ответственного сотрудника клиенту."""
    client = get_object_or_404(Client, pk=client_id)
    employee_id = request.POST.get("employee_id")
    if not employee_id:
        return HttpResponseBadRequest("employee_id required")

    new_employee = get_object_or_404(Employee, pk=employee_id, is_active=True)

    # Определяем предыдущего ответственного из query param (выставляется бэйджем)
    prev_id = request.POST.get("prev_employee_id") or request.GET.get("current")
    prev_employee = Employee.objects.filter(pk=prev_id).select_related("user").first() if prev_id else None

    _, created = ClientEmployee.objects.get_or_create(client=client, employee=new_employee)

    # Лог события только если назначение реально изменилось
    if created or (prev_employee and prev_employee != new_employee):
        from apps.crm.models import ClientEvent
        try:
            actor = Employee.objects.get(user=request.user)
        except Employee.DoesNotExist:
            actor = None

        if prev_employee and prev_employee != new_employee:
            desc = (
                f"Ответственный изменён: {prev_employee.user.get_full_name()} → "
                f"{new_employee.user.get_full_name()}"
            )
        else:
            desc = f"Назначен ответственный: {new_employee.user.get_full_name()}"

        ClientEvent.objects.create(
            client=client,
            event_type="employee_assigned",
            description=desc,
            employee=actor,
        )

    # Возвращаем обновлённый бэйдж
    return render(request, "crm/partials/assign_employee_badge.html", {
        "client": client,
        "emp": new_employee,
    })


@login_required
@require_POST
def client_move(request, client_id):
    """Drag-and-drop: смена статуса клиента между колонками канбана."""
    client = get_object_or_404(Client, pk=client_id)
    new_status = request.POST.get("status", "")
    valid = {c[0] for c in Client.STATUS_CHOICES}
    if new_status not in valid:
        return HttpResponseBadRequest("Invalid status")

    old_status = client.status
    if old_status == new_status:
        return HttpResponse(status=204)

    client.status = new_status
    client.save(update_fields=["status"])

    STATUS_LABELS = dict(Client.STATUS_CHOICES)
    try:
        actor = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        actor = None

    from apps.crm.models import ClientEvent
    ClientEvent.objects.create(
        client=client,
        event_type="status_change",
        description=(
            f"Статус изменён: {STATUS_LABELS.get(old_status, old_status)} "
            f"→ {STATUS_LABELS.get(new_status, new_status)}"
        ),
        old_value=STATUS_LABELS.get(old_status, old_status),
        new_value=STATUS_LABELS.get(new_status, new_status),
        employee=actor,
    )
    return HttpResponse(status=204)


@login_required
def service_employee_picker(request, pk):
    """HTMX: ��одалка назначения исполнителей услуги."""
    service = get_object_or_404(Service, pk=pk)
    return _render_assign_modal(request, service)


def _render_assign_modal(request, service):
    assigned_ids = set(
        service.employee_states.values_list("employee_id", flat=True)
    )
    departments = (
        Department.objects.filter(is_active=True)
        .prefetch_related(
            Prefetch(
                "employees",
                queryset=Employee.objects.filter(is_active=True)
                    .select_related("user")
                    .order_by("user__last_name", "user__first_name"),
            )
        )
        .order_by("name")
    )
    no_dept = Employee.objects.filter(
        is_active=True, department__isnull=True
    ).select_related("user").order_by("user__last_name", "user__first_name")

    return render(request, "crm/partials/service_assign_modal.html", {
        "service": service,
        "departments": departments,
        "no_dept": no_dept,
        "assigned_ids": assigned_ids,
    })


@login_required
@require_POST
def service_employee_toggle(request, pk):
    """HTMX: добавить или убрать исполнителя услуги."""
    service     = get_object_or_404(Service, pk=pk)
    employee_id = request.POST.get("employee_id")
    employee    = get_object_or_404(Employee, pk=employee_id, is_active=True)

    state = ServiceEmployeeState.objects.filter(service=service, employee=employee).first()
    if state:
        state.delete()
        action = "unassigned"
    else:
        ServiceEmployeeState.objects.create(service=service, employee=employee)
        action = "assigned"

    try:
        actor = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        actor = None

    ServiceLog.objects.create(
        service=service,
        employee=actor,
        action=action,
    )

    if service.client_id:
        emp_name = employee.user.get_full_name() or employee.user.username
        svc_label = service.numb_dogovor or service.name.short_name
        if action == "assigned":
            desc = f"Услуга {svc_label}: назначен исполнитель — {emp_name}"
        else:
            desc = f"Услуга {svc_label}: снят исполнитель — {emp_name}"
        ClientEvent.objects.create(
            client_id=service.client_id,
            event_type="employee_assigned",
            description=desc,
            employee=actor,
        )

    assigned_ids = list(
        service.employee_states.values_list("employee_id", flat=True)
    )
    assigned_count = len(assigned_ids)

    # JSON-ответ для fetch из модалки
    if request.POST.get("from_modal"):
        from django.http import JsonResponse as _JR
        states_qs = service.employee_states.select_related("employee__user", "status").all()
        from apps.crm.templatetags.crm_tags import short_name as _sn
        badges_html = "".join(
            f'<span class="badge badge-outline text-xs" '
            f'title="{st.employee.user.get_full_name()}">'
            f'{_sn(st.employee)}</span>'
            for st in states_qs
        ) or '<span class="text-xs text-base-content/40">исполнителей нет</span>'
        return _JR({
            "assigned": action == "assigned",
            "employee_id": str(employee_id),
            "assigned_count": assigned_count,
            "badges_html": badges_html,
        })

    states = service.employee_states.select_related("employee__user", "status").all()
    return render(request, "crm/partials/service_employee_list.html", {
        "service": service,
        "states": states,
    })
