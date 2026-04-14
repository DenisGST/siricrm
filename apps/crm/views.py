from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string

from apps.crm.models import *
from django.db import models
from django.db.models import Q, Prefetch
from django.utils import timezone

from .models import Client, Message
from .forms import ClientForm
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

    html = render_to_string(
        "crm/partials/telegram_message.html",
        {"msg": msg},
        request=request,
    )
    return HttpResponse(html)


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

    html = render_to_string(
        "crm/partials/telegram_message.html",
        {"msg": msg},
        request=request,
    )
    return HttpResponse(html)


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

    # обычный GET / HX без page → полная панель с последними сообщениями
    return render(
        request,
        "crm/partials/telegram_chat_panel.html",
        {"client": client, "page_obj": page_obj, "messages": page_obj.object_list, "search_q": search_q},
    )


@login_required
def telegram_clients_list(request):
    page_number = request.GET.get("page") or 1
    query = (request.GET.get("q") or "").strip()

    qs = Client.objects.all().order_by("-last_message_at")

    if query:
        qs = qs.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(username__icontains=query)
            | Q(phone__icontains=query)
        )

    paginator = Paginator(qs, CLIENTS_PER_PAGE)
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "has_next": page_obj.has_next(),
        "next_page_number": page_obj.next_page_number() if page_obj.has_next() else None,
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


@login_required
def kanban(request):
    # версия с четырьмя колонками
    leads = Client.objects.filter(status="lead").prefetch_related("employees")
    actives = Client.objects.filter(status="active").prefetch_related("employees")
    inactives = Client.objects.filter(status="inactive").select_related(
        "assigned_employee"
    )
    closeds = Client.objects.filter(status="closed").prefetch_related("employees")

    return render(
        request,
        "crm/kanban.html",
        {
            "leads": leads,
            "actives": actives,
            "inactives": inactives,
            "closeds": closeds,
        },
    )


@login_required
def kanban_column(request, status):
    clients = (
        Client.objects.filter(status=status)
        .prefetch_related("employees")
        .order_by("-last_message_at")
    )

    return render(request, "crm/partials/kanban_column.html", {"clients": clients})


@login_required
def client_create(request):
    """
    Создание клиента с поддержкой HTMX.
    GET:
    - обычный запрос -> crm/clients/form.html (полная страница в content-area)
    POST:
    - HTMX (HX-Request) -> crm/kanban.html (обновлённый канбан)
    - обычный POST -> redirect на clients_list
    """
    if request.method == "POST":
        form = ClientForm(request.POST)
        if form.is_valid():
            form.save()

            if request.headers.get("HX-Request"):
                leads = Client.objects.filter(status="lead").select_related(
                    "assigned_employee"
                )
                actives = Client.objects.filter(status="active").select_related(
                    "assigned_employee"
                )
                inactives = Client.objects.filter(
                    status="inactive"
                ).prefetch_related("employees")
                closeds = Client.objects.filter(status="closed").select_related(
                    "assigned_employee"
                )

                return render(
                    request,
                    "crm/kanban.html",
                    {
                        "leads": leads,
                        "actives": actives,
                        "inactives": inactives,
                        "closeds": closeds,
                    },
                )

            return redirect("clients_list")
    else:
        form = ClientForm()

    return render(
        request,
        "crm/clients/form.html",
        {"form": form},
    )


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
                    '<div id="client-edit-success" class="alert alert-success text-sm">✅ Сохранено</div>',
                    headers={"HX-Trigger": "clientUpdated"},
                )
            return redirect("chat", client_id=client_id)
        else:
            if request.headers.get("HX-Request"):
                return render(request, "crm/partials/client_edit_modal.html", {
                    "form": form, "client": client
                })
    else:
        form = ClientForm(instance=client)

    return render(request, "crm/partials/client_edit_modal.html", {
        "form": form, "client": client
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

    # Transfer channel IDs (only if source is missing them)
    if not source.telegram_id and target.telegram_id:
        source.telegram_id = target.telegram_id
    if not source.max_chat_id and target.max_chat_id:
        source.max_chat_id = target.max_chat_id

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
    for emp in target.employees.all():
        source.employees.add(emp)

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
