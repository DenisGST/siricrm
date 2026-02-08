import boto3
from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string

from apps.crm.models import *
from django.db import models
from .models import Client
from .forms import ClientForm
from .models import Employee
from .models import EmployeeLog
from .models import Message
from django.db.models import Prefetch
from django.db import transaction
from apps.auth_telegram.models import TelegramUser  # если нужно
from apps.realtime.utils import push_chat_message, push_toast
from .telegram_sender import send_from_crm_sync

CLIENTS_PER_PAGE = 20

@login_required
@require_POST
def telegram_send_message(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    content = (request.POST.get("content") or "").strip()
    up_file = request.FILES.get("file")

    # если ничего не ввели и файл не выбрали — можно вернуть 400 или просто пустой ответ
    if not content and not up_file:
        messages_qs = Message.objects.filter(client=client).order_by("created_at")
        return render(
            request,
            "crm/partials/telegram_chat_panel.html",
            {"client": client, "messages": messages_qs},
        )

    try:
        employee = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        employee = None

    # обновляем дату последнего сообщения у клиента
    client.last_message_at = timezone.now()
    client.save(update_fields=["last_message_at"])

    # отправка в Telegram + создание Message
    send_from_crm_sync(
        client=client,
        text=content or None,
        file=up_file,
        employee=employee,
    )

    # берём только что созданное сообщение (последнее исходящее для этого клиента)
    msg = (
        Message.objects.filter(client=client)
        .order_by("-created_at")
        .first()
    )
    if msg:
        push_chat_message(msg)
        push_toast(request.user, "Сообщение клиенту отправлено", level="success")

    if not msg:
        return HttpResponseBadRequest("No message created")

    # рендерим HTML одного сообщения
    html = render_to_string(
        "crm/partials/telegram_message.html",
        {"msg": msg},
        request=request,
    )

    # возвращаем только его: форма с hx-target="#telegram-chat-messages"
    # и hx-swap="beforeend" добавит его в конец ленты
    return HttpResponse(
        render_to_string("crm/partials/telegram_message.html", {"msg": msg}, request=request)
    )

@login_required
def telegram_chat_for_client(request, client_id):
    client = get_object_or_404(Client, pk=client_id)

    # последние N сообщений, например 50
    messages_qs = (
        Message.objects
        .filter(client=client)
        .order_by("created_at")  # в порядке диалога
    )[:200]  # при желании ограничь

    context = {
        "client": client,
        "messages": messages_qs,
    }
    return render(request, "crm/partials/telegram_chat_panel.html", context)


@login_required
def telegram_clients_list(request):
    page_number = request.GET.get("page", 1)

    qs = Client.objects.all()

    # можно отфильтровать только «живых» клиентов, если нужно:
    # qs = qs.filter(status__in=["lead", "active"])

    paginator = Paginator(qs, CLIENTS_PER_PAGE)
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "has_next": page_obj.has_next(),
        "next_page_number": page_obj.next_page_number() if page_obj.has_next() else None,
    }

    return render(request, "crm/partials/telegram_clients_list.html", context)


def dashboard_view(request):
    bot_status = get_bot_status()
    return render(request, "dashboard.html", {
        "bot_status": bot_status,
    })

@login_required
def chat(request, client_id):
    client = get_object_or_404(
        Client.objects.select_related("assigned_employee")
        .prefetch_related(
            Prefetch(
                "messages",
                queryset=Message.objects.select_related("employee").order_by("created_at"),
            )
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
    telegram_user = getattr(request.user, "telegram_user", None)

    return render(
        request,
        "dashboard.html",
        {
            "telegram_user": telegram_user,
            "telegram_bot_username": getattr(settings, "TELEGRAM_BOT_USERNAME", ""),
        },
    )

@login_required
def kanban(request):
    leads = Client.objects.filter(status="lead").select_related("assigned_employee")
    actives = Client.objects.filter(status="active").select_related("assigned_employee")
    inactives = Client.objects.filter(status="inactive").select_related("assigned_employee")
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
def client_create(request):
    """
    Создание клиента с поддержкой HTMX.
    GET:
      - обычный запрос -> crm/clients/form.html (полная страница в content-area)
    POST:
      - HTMX (HX-Request) -> crm/clients/list_partial.html (обновлённая таблица)
      - обычный POST -> redirect на clients_list
    """

    if request.method == "POST":
        form = ClientForm(request.POST)
        if form.is_valid():
            form.save()

            if request.headers.get("HX-Request"):
                # Перерисовать Kanban после создания клиента
                leads = Client.objects.filter(status="lead").select_related("assigned_employee")
                actives = Client.objects.filter(status="active").select_related("assigned_employee")
                inactives = Client.objects.filter(status="inactive").select_related("assigned_employee")
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
def kanban(request):
    clients = Client.objects.filter(status__in=['lead', 'active'])
    return render(request, 'crm/kanban.html', {'clients': clients})

@login_required
def kanban_column(request, status):
    if status not in dict(Client.STATUS_CHOICES):
        return HttpResponseBadRequest("Invalid status")

    clients = Client.objects.filter(status=status).select_related("assigned_employee")
    return render(request, "crm/partials/kanban_column.html", {"clients": clients})

@login_required
def clients_list(request):
    search = request.GET.get("search", "").strip()
    qs = Client.objects.select_related("assigned_employee")

    if search:
        qs = qs.filter(
            models.Q(first_name__icontains=search)
            | models.Q(last_name__icontains=search)
            | models.Q(username__icontains=search)
            | models.Q(phone__icontains=search)
            | models.Q(email__icontains=search)
        )

    paginator = Paginator(qs.order_by("-last_message_at"), 25)
    page_number = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_number)

    template = (
        "crm/clients/list_partial.html"
        if request.headers.get("HX-Request")
        else "crm/clients/list.html"
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
def employees_online_count(request):
    count = Employee.objects.filter(is_online=True).count()
    return HttpResponse(count)

@login_required
def clients_active_count(request):
    count = Client.objects.filter(status='active').count()
    return HttpResponse(count)

@login_required
def messages_new_count(request):
    count = Message.objects.filter(is_read=False).count()
    return HttpResponse(count)

@login_required
def lead_count(request):
    count = Client.objects.filter(status='lead').count()
    return HttpResponse(count)