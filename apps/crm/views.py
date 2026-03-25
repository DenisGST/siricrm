import boto3
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
from django.db import transaction
from django.utils import timezone

from .models import Client, Message
from .forms import ClientForm
from apps.core.models import Employee, EmployeeLog
from apps.realtime.utils import  push_toast
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
        msg.save(update_fields=["channel", "telegram_date"])
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
    qs = (
        Message.objects.filter(client=client)
        .select_related("employee")
        .order_by("telegram_date", "id")
    )

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
        {"client": client, "page_obj": page_obj, "messages": page_obj.object_list},
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
    leads = Client.objects.filter(status="lead").select_related("assigned_employee")
    actives = Client.objects.filter(status="active").select_related("assigned_employee")
    inactives = Client.objects.filter(status="inactive").select_related(
        "assigned_employee"
    )
    closeds = Client.objects.filter(status="closed").select_related("assigned_employee")

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
                ).select_related("assigned_employee")
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
    search = request.GET.get("search", "").strip()
    qs = Employee.objects.select_related("user", "department")

    if search:
        qs = qs.filter(
            models.Q(user__first_name__icontains=search)
            | models.Q(user__last_name__icontains=search)
            | models.Q(telegram_username__icontains=search)
            | models.Q(department__name__icontains=search)
        )

    paginator = Paginator(qs.order_by("-last_seen"), 25)
    page_number = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_number)

    template = (
        "crm/employees/list_partial.html"
        if request.headers.get("HX-Request")
        else "crm/employees/list.html"
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
