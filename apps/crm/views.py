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

from .models import Client, Message, Address, LegalEntity, ClientEmployee
from .forms import ClientForm, LegalEntityForm
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
        ClientEmployee.objects.filter(
            client=client, employee=employee,
        ).update(messenger_status="waiting", status_changed_at=timezone.now())
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
        ClientEmployee.objects.filter(
            client=client, employee=employee,
        ).update(messenger_status="waiting", status_changed_at=timezone.now())
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

    if query:
        qs = qs.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(username__icontains=query)
            | Q(phone__icontains=query)
        )

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

    context = {
        "page_obj": page_obj,
        "has_next": page_obj.has_next(),
        "next_page_number": page_obj.next_page_number() if page_obj.has_next() else None,
        "scope": scope,
        "query": query,
        "sort": sort,
    }

    return render(request, "crm/partials/telegram_clients_list.html", context)


def dashboard_view(request):
    from apps.crm.bot_status import get_bot_status

    bot_status = get_bot_status()
    return render(
        request,
        "dashboard.html",
        {
            "bot_status": bot_status,
        },
    )


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
    employee_id = request.GET.get("employee") or ""
    ms_status = request.GET.get("ms_status") or ""
    created_from = request.GET.get("created_from") or ""
    created_to = request.GET.get("created_to") or ""

    base_qs = Client.objects.all()
    if employee_id:
        base_qs = base_qs.filter(employees__id=employee_id)
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

    leads = list(base_qs.filter(status="lead").prefetch_related("employees"))
    actives = list(base_qs.filter(status="active").prefetch_related("employees"))
    inactives = list(base_qs.filter(status="inactive").select_related("assigned_employee"))
    closeds = list(base_qs.filter(status="closed").prefetch_related("employees"))

    for group in (leads, actives, inactives, closeds):
        _annotate_ms_status(group, request.user)

    context = {
        "leads": leads, "actives": actives,
        "inactives": inactives, "closeds": closeds,
        "filter_employee": employee_id,
        "filter_ms_status": ms_status,
        "filter_created_from": created_from,
        "filter_created_to": created_to,
        "employees_all": Employee.objects.select_related("user").order_by("user__last_name", "user__first_name"),
    }
    template = "crm/kanban.html"
    if request.headers.get("HX-Request"):
        template = "crm/kanban.html"
    return render(request, template, context)


@login_required
def kanban_column(request, status):
    employee_id = request.GET.get("employee") or ""
    ms_status = request.GET.get("ms_status") or ""
    created_from = request.GET.get("created_from") or ""
    created_to = request.GET.get("created_to") or ""

    qs = Client.objects.filter(status=status)
    if employee_id:
        qs = qs.filter(employees__id=employee_id)
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

    clients = list(qs.prefetch_related("employees").order_by("-last_message_at"))
    _annotate_ms_status(clients, request.user)
    return render(request, "crm/partials/kanban_column.html", {"clients": clients})


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
    ).order_by("last_name", "first_name")[:8]

    legal_entities = LegalEntity.objects.filter(
        Q(name__icontains=q) | Q(inn__icontains=q) | Q(ogrn__icontains=q)
    ).order_by("name")[:5]

    messages = Message.objects.filter(
        content__icontains=q
    ).select_related("client").order_by("-created_at")[:5]

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
    from apps.crm.models import LegalEntityKind

    search = request.GET.get("search", "").strip()
    f_kind = request.GET.get("kind", "").strip()
    f_status = request.GET.get("status", "").strip()
    f_entity_type = request.GET.get("entity_type", "").strip()
    sort = (request.GET.get("sort") or "name").strip()

    ALLOWED_SORT = {
        "name", "kind__short_name", "entity_type", "inn", "ogrn",
        "director_name", "status", "brand",
    }
    sort_key = sort.lstrip("-")
    if sort_key not in ALLOWED_SORT:
        sort = "name"

    qs = LegalEntity.objects.select_related("kind").all()

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
        "sort": sort,
        "kinds": LegalEntityKind.objects.order_by("name"),
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
