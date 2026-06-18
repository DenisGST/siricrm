from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404, redirect
from django.core.cache import cache
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string
import logging

logger = logging.getLogger(__name__)

from apps.crm.models import *
from django.db import models, transaction
from django.db.models import Q, Prefetch, F
from django.utils import timezone

from .models import (
    Client, Message, Address, LegalEntity, ClientEmployee,
    Service, ServiceName, PaymentProcedure, ServiceCommonStatus,
    ServiceEmployeeStatus, ServiceTag, ServiceEmployeeState,
    ServiceTagAssignment, ServiceLog, ClientLogEntry,
    EventType, ActionType,
)
from . import client_log
from .forms import ClientForm, LegalEntityForm, ServiceForm
from apps.core.models import Employee, EmployeeLog, Department
from apps.core.permissions import is_management, is_references_access
from apps.telegram.telegram_sender import create_message_and_store_file
from rules.contrib.views import permission_required as rules_permission_required
from .tasks import send_telegram_message_task
from apps.maxchat.tasks import send_max_message_task
from apps.whatsapp.tasks import send_whatsapp_message_task


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
@require_POST
def whatsapp_send_message(request, client_id):
    """Отправить WhatsApp-сообщение через 1msg.io. Структура — копия
    ``max_send_message``: создаём Message(channel='whatsapp'), кидаем в
    Celery, возвращаем HTMX-партиал."""
    from apps.crm.phone_utils import find_client_by_phone, normalize_phone

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

    # Проверяем что у клиента есть WA-номер (в любом из вариантов)
    has_wa = bool(
        client.whatsapp_phone
        or client.phones.filter(purpose__in=["whatsapp", "primary"]).exists()
        or client.phone
    )
    if not has_wa:
        return HttpResponseBadRequest("Client has no WhatsApp phone")

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
        msg.channel = "whatsapp"
        msg.telegram_date = timezone.now()
        msg.reply_to = reply_to_msg
        msg.save(update_fields=["channel", "telegram_date", "reply_to"])
    else:
        msg = Message.objects.create(
            client=client,
            employee=employee,
            content=content,
            direction="outgoing",
            channel="whatsapp",
            message_type="text",
            telegram_date=timezone.now(),
            is_sent=False,
            reply_to=reply_to_msg,
        )

    send_whatsapp_message_task.delay(str(msg.id))

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


def _approved_wa_templates():
    """Активные WA-шаблоны, одобренные Meta."""
    from apps.crm.models import MessageTemplate
    qs = MessageTemplate.objects.filter(
        is_active=True, whatsapp_meta_status="approved",
    ).order_by("name")
    return [t for t in qs if "whatsapp" in (t.channels or [])]


def _render_wa_template_body(body: str, params: list) -> str:
    """Подставить значения в {{1}}, {{2}}… по порядку (для показа в чате)."""
    import re

    def repl(m):
        idx = int(m.group(1)) - 1
        return params[idx] if 0 <= idx < len(params) else m.group(0)

    return re.sub(r"{{\s*(\d+)\s*}}", repl, body or "")


def _render_crm_template(body: str, client, employee) -> str:
    """Отрендерить тело TG/MAX-шаблона с CRM-плейсхолдерами
    ({{ client.first_name }} и т.п.). Шаблоны заводят доверенные сотрудники."""
    from django.template import Template, Context
    try:
        return Template(body or "").render(Context({"client": client, "employee": employee}))
    except Exception:
        return body or ""


@login_required
def whatsapp_template_picker(request, client_id):
    """Единая модалка шаблонов для всех каналов (Telegram / MAX / WhatsApp).

    Табы = каналы (только доступные клиенту). В каждом — select шаблонов;
    при выборе показываются «настройки»: для WA — поля переменных {{N}}
    (approved-шаблоны, sendTemplate), для TG/MAX — редактируемый текст
    (обычное сообщение с подставленными CRM-плейсхолдерами).
    """
    import re
    from apps.crm.models import MessageTemplate

    client = get_object_or_404(Client, pk=client_id)
    try:
        employee = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        employee = None

    channels = []
    if client.telegram_id:
        channels.append({"key": "telegram", "label": "Telegram", "color": "#0078d4", "items": []})
    if client.max_chat_id:
        channels.append({"key": "max", "label": "MAX", "color": "#8764b8", "items": []})
    if client.whatsapp_phone or client.phone:
        channels.append({"key": "whatsapp", "label": "WhatsApp", "color": "#22c55e", "items": []})

    tpl_qs = MessageTemplate.objects.filter(is_active=True).order_by("name")
    tpl_fields = {}  # tpl_id -> {is_wa, params, rendered, preview}
    for ch in channels:
        key = ch["key"]
        for t in tpl_qs:
            if key not in (t.channels or []):
                continue
            if key == "whatsapp" and t.whatsapp_meta_status != "approved":
                continue
            tid = str(t.id)
            ch["items"].append({"id": tid, "name": t.name})
            if tid in tpl_fields:
                continue
            if key == "whatsapp":
                nums = sorted({int(n) for n in re.findall(r"{{\s*(\d+)\s*}}", t.body or "")})
                schema = t.whatsapp_params_schema or []
                params = []
                for i, n in enumerate(nums):
                    hint = schema[i].get("example", "") if i < len(schema) and isinstance(schema[i], dict) else ""
                    default = client.first_name if (i == 0 and client.first_name) else ""
                    params.append({"n": n, "hint": hint, "default": default})
                tpl_fields[tid] = {"is_wa": True, "params": params, "rendered": "", "preview": t.body}
            else:
                rendered = _render_crm_template(t.body, client, employee)
                tpl_fields[tid] = {"is_wa": False, "params": [], "rendered": rendered, "preview": rendered}

    initial = request.GET.get("channel") or ""
    keys = [c["key"] for c in channels]
    if initial not in keys:
        initial = keys[0] if keys else ""

    return render(request, "crm/partials/chat_template_picker.html", {
        "client": client, "channels": channels, "initial": initial,
        "tpl_fields": tpl_fields,
    })


@login_required
@require_POST
def whatsapp_send_template(request, client_id):
    """Отправить одобренный WA-шаблон (sendTemplate) — работает вне окна 24ч."""
    from apps.crm.models import MessageTemplate
    from apps.whatsapp.tasks import send_whatsapp_template_task

    client = get_object_or_404(Client, pk=client_id)
    tpl = get_object_or_404(MessageTemplate, pk=request.POST.get("template_id"))
    if tpl.whatsapp_meta_status != "approved":
        return HttpResponseBadRequest("Шаблон не одобрен Meta")

    params = [p.strip() for p in request.POST.getlist("param")]
    rendered = _render_wa_template_body(tpl.body, params)

    try:
        employee = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        employee = None

    client.last_message_at = timezone.now()
    client.save(update_fields=["last_message_at"])

    msg = Message.objects.create(
        client=client,
        employee=employee,
        content=rendered,
        direction="outgoing",
        channel="whatsapp",
        message_type="text",
        telegram_date=timezone.now(),
        is_sent=False,
        message_template=tpl,
        template_params=params,
    )
    send_whatsapp_template_task.delay(str(msg.id))

    if employee:
        ClientEmployee.objects.update_or_create(
            client=client, employee=employee,
            defaults={"messenger_status": "waiting", "status_changed_at": timezone.now()},
        )
        from apps.realtime.utils import push_messenger_status_update
        push_messenger_status_update(client)

    html = render_to_string(
        "crm/partials/telegram_message.html", {"msg": msg}, request=request,
    )
    resp = HttpResponse(html)
    resp["HX-Trigger"] = "messengerStatusChanged"
    return resp


@login_required
@require_POST
def chat_send_template(request, client_id):
    """Единая отправка шаблона по выбранному каналу из общей модалки.

    WhatsApp → approved WABA-шаблон (sendTemplate, вне 24ч-окна).
    Telegram / MAX → обычное сообщение с финальным текстом (оператор мог
    отредактировать в модалке)."""
    from apps.crm.models import MessageTemplate

    client = get_object_or_404(Client, pk=client_id)
    channel = request.POST.get("channel")
    if channel not in ("telegram", "max", "whatsapp"):
        return HttpResponseBadRequest("Неизвестный канал")

    try:
        employee = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        employee = None

    # WhatsApp — через approved-шаблон (sendTemplate)
    if channel == "whatsapp":
        from apps.whatsapp.tasks import send_whatsapp_template_task
        tpl = get_object_or_404(MessageTemplate, pk=request.POST.get("template_id"))
        if tpl.whatsapp_meta_status != "approved":
            return HttpResponseBadRequest("Шаблон не одобрен Meta")
        params = [p.strip() for p in request.POST.getlist("param")]
        rendered = _render_wa_template_body(tpl.body, params)
        client.last_message_at = timezone.now()
        client.save(update_fields=["last_message_at"])
        msg = Message.objects.create(
            client=client, employee=employee, content=rendered,
            direction="outgoing", channel="whatsapp", message_type="text",
            telegram_date=timezone.now(), is_sent=False,
            message_template=tpl, template_params=params,
        )
        send_whatsapp_template_task.delay(str(msg.id))

    # Telegram / MAX — обычный текст
    else:
        text = (request.POST.get("text") or "").strip()
        if not text:
            return HttpResponseBadRequest("Пустой текст")
        if channel == "telegram" and not client.telegram_id:
            return HttpResponseBadRequest("У клиента нет Telegram")
        if channel == "max" and not client.max_chat_id:
            return HttpResponseBadRequest("У клиента нет MAX")
        tpl = MessageTemplate.objects.filter(pk=request.POST.get("template_id")).first()
        client.last_message_at = timezone.now()
        client.save(update_fields=["last_message_at"])
        msg = Message.objects.create(
            client=client, employee=employee, content=text,
            direction="outgoing", channel=channel, message_type="text",
            telegram_date=timezone.now(), is_sent=False, message_template=tpl,
        )
        if channel == "telegram":
            from apps.crm.tasks import send_telegram_message_task
            send_telegram_message_task.delay(str(msg.id))
        else:
            from apps.maxchat.tasks import send_max_message_task
            send_max_message_task.delay(str(msg.id))

    if employee:
        ClientEmployee.objects.update_or_create(
            client=client, employee=employee,
            defaults={"messenger_status": "waiting", "status_changed_at": timezone.now()},
        )
        from apps.realtime.utils import push_messenger_status_update
        push_messenger_status_update(client)

    html = render_to_string(
        "crm/partials/telegram_message.html", {"msg": msg}, request=request,
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

    # Канал отправки по Enter = канал последнего входящего (по нему пришло
    # последнее сообщение), затем последнего вообще; только среди доступных.
    avail = []
    if client.telegram_id:
        avail.append("telegram")
    if client.max_chat_id:
        avail.append("max")
    if client.whatsapp_phone or client.phone:
        avail.append("whatsapp")
    last_in = (
        Message.objects.filter(client=client, direction="incoming")
        .order_by("-telegram_date", "-id").values_list("channel", flat=True).first()
    )
    last_any = (
        Message.objects.filter(client=client)
        .order_by("-telegram_date", "-id").values_list("channel", flat=True).first()
    )
    default_channel = ""
    for cand in (last_in, last_any):
        if cand in avail:
            default_channel = cand
            break
    if not default_channel and avail:
        default_channel = avail[0]

    return render(
        request,
        "crm/partials/telegram_chat_panel.html",
        {"client": client, "page_obj": page_obj, "messages": page_obj.object_list,
         "search_q": search_q, "messenger_status": messenger_status,
         "default_channel": default_channel},
    )


def _telegram_clients_base_qs(emp, scope):
    """Базовый queryset клиентов для scope ('mine'/'dept'/'all'). Без search/sort/paginate.

    «Мои» / «Отдел» учитывают клиента и как ответственного
    (Client.employees), и как исполнителя по любой его услуге
    (Service.employees).
    """
    from django.db.models import Q
    qs = Client.objects.all()
    if emp:
        if scope == "mine":
            qs = qs.filter(
                Q(employees=emp) | Q(services__employees=emp)
            ).distinct()
        elif scope == "dept" and emp.department_id:
            qs = qs.filter(
                Q(employees__department_id=emp.department_id)
                | Q(services__employees__department_id=emp.department_id)
            ).distinct()
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

    # Backend-защита: scope='all' доступен только тем, кому видна вся компания
    # (admin/head_dep/managing_partner/owner/Department.sees_all_clients).
    from apps.core.permissions import can_view_all_clients
    if scope == "all" and not can_view_all_clients(request.user):
        scope = "mine"

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
    # Для сортировки по last_message_at NULL'ы держим в конце — клиенты без
    # сообщений не должны вытеснять реально активных наверх (NULLS FIRST в
    # Postgres для DESC поднимал их). Для остальных сортировок — обычный
    # order_by.
    if sort == "-last_message_at":
        qs = qs.order_by(F("last_message_at").desc(nulls_last=True), "id")
    elif sort == "last_message_at":
        qs = qs.order_by(F("last_message_at").asc(nulls_last=True), "id")
    else:
        qs = qs.order_by(sort, "id")

    search_q = None
    if query:
        search_q = (
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(username__icontains=query)
            | Q(phone__icontains=query)
            | Q(phones__phone__icontains=query)
        )
        qs = qs.filter(search_q).distinct()

    paginator = Paginator(qs, CLIENTS_PER_PAGE)
    page_obj = paginator.get_page(page_number)

    # «Pin» клиент: если в запросе есть pin_client_id и этот клиент НЕ попал
    # в текущий scope/search — допольним его в начало списка (только page=1).
    # Используется при открытии чата конкретного клиента, чтобы в левой
    # колонке гарантированно был виден активный (подсвеченный) клиент.
    pinned_client = None
    pin_client_id = (request.GET.get("pin_client_id") or "").strip()
    if pin_client_id and page_obj.number == 1:
        page_ids = {str(c.pk) for c in page_obj.object_list}
        if pin_client_id not in page_ids:
            try:
                pinned_client = (
                    Client.objects.visible_to(request.user)
                    .filter(pk=pin_client_id)
                    .first()
                )
            except (ValueError, TypeError):
                pinned_client = None

    # Статусы мессенджера для текущего сотрудника
    clients_for_status = list(page_obj.object_list)
    if pinned_client is not None:
        clients_for_status.append(pinned_client)
    if emp:
        statuses = dict(
            ClientEmployee.objects.filter(
                employee=emp, client__in=clients_for_status,
            ).values_list("client_id", "messenger_status")
        )
        for c in clients_for_status:
            c.ms_status = statuses.get(c.pk, "")
    else:
        for c in clients_for_status:
            c.ms_status = ""

    # Каналы, по которым у клиента есть сообщения — для значков Т/М/W в списке
    # (серый = сообщений из канала не было). Один запрос на всю страницу.
    channel_map = {}
    for cid_, ch in (
        Message.objects.filter(client__in=clients_for_status)
        .values_list("client_id", "channel").distinct()
    ):
        channel_map.setdefault(cid_, set()).add(ch)
    for c in clients_for_status:
        chans = channel_map.get(c.pk, set())
        c.has_telegram = "telegram" in chans
        c.has_max = "max" in chans
        c.has_whatsapp = "whatsapp" in chans

    # Регион(ы) услуг клиента — для отображения в списке. Один запрос на страницу
    # (без N+1). У клиента может быть несколько услуг в разных регионах — собираем
    # уникальные названия.
    region_map = {}
    for cid_, rname in (
        Service.objects.filter(client__in=clients_for_status, region__isnull=False)
        .values_list("client_id", "region__name").distinct()
    ):
        if rname and rname not in region_map.setdefault(cid_, []):
            region_map[cid_].append(rname)
    for c in clients_for_status:
        c.region_label = ", ".join(region_map.get(c.pk, []))

    if pinned_client is not None:
        page_obj.object_list = [pinned_client] + list(page_obj.object_list)

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

    _pfetch = ["employees", "services__name", "services__common_status"]
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
    q            = (request.GET.get("q") or "").strip()
    cid          = (request.GET.get("cid") or "").strip()

    qs = Client.objects.visible_to(request.user).filter(status=status)
    if cid:
        # «Только этот клиент» из главного поиска — точная фильтрация по id
        # (иначе у клиента без фамилии фильтр по ФИО показывал всех тёзок).
        qs = qs.filter(pk=cid)
    elif q:
        # Разбиваем «Каныгин Денис» на слова — каждое слово должно
        # совпасть с одним из полей (AND по словам, OR по полям).
        for word in q.split():
            qs = qs.filter(
                Q(first_name__icontains=word)
                | Q(last_name__icontains=word)
                | Q(patronymic__icontains=word)
                | Q(phone__icontains=word)
                | Q(phones__phone__icontains=word)
            )
        qs = qs.distinct()
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

    # Сортировка: клиенты с перепиской — сверху по дате сообщения; без неё
    # (импортированные, ещё без сообщений) — ниже, в стабильном порядке
    # по дате создания. nulls_last обязателен — иначе пустые last_message_at
    # уезжают в начало колонки.
    # Последнее сообщение — через Subquery, чтобы не получить N+1 на колонке.
    from django.db.models import OuterRef, Subquery
    from apps.crm.models import Message
    last_msg = Message.objects.filter(client=OuterRef("pk")).order_by("-created_at")
    from django.db.models import Prefetch as _Prefetch
    qs = qs.prefetch_related(
        # для client.primary_employee (детерминированный ответственный) без N+1
        _Prefetch("client_employees", queryset=ClientEmployee.objects.select_related("employee__user")),
        "services__name", "services__common_status",
    ).annotate(
        last_message_content=Subquery(last_msg.values("content")[:1]),
    ).order_by(
        F("last_message_at").desc(nulls_last=True), "-created_at",
    )

    # Постраничная подгрузка — иначе на больших колонках (после импорта из
    # Bubble) рендер всех карточек разом вешает страницу. ВАЖНО: total
    # считаем через qs.count() и режем срезом, чтобы не тащить весь
    # queryset в память (это и был причиной тормозов).
    # PAGE_SIZE небольшой: 5 колонок × карточка (~185 строк шаблона) грузились
    # на старте дашборда жадно (~1.7 МБ). Теперь начальная отрисовка лёгкая,
    # остальное догружается на скролл (intersect «load more» в kanban_column.html).
    PAGE_SIZE = 12
    total = qs.count()
    try:
        offset = max(int(request.GET.get("offset") or 0), 0)
    except (TypeError, ValueError):
        offset = 0
    shown = list(qs[offset:offset + PAGE_SIZE])
    _annotate_ms_status(shown, request.user)
    next_offset = offset + PAGE_SIZE
    has_more = next_offset < total

    # column_id — DOM-идентификатор колонки (для intersect-root, OOB-счётчика,
    # «Показать ещё»). Обычно совпадает со status, но для динамической
    # колонки «Архивные» (где select переключает status) колонка остаётся
    # одна — id всегда "archive", а status меняется.
    column_id = request.GET.get("column_id") or status
    return render(request, "crm/partials/kanban_column.html", {
        "clients":   shown,
        "status":    status,
        "column_id": column_id,
        "count":     total,
        "offset":    offset,
        "has_more":  has_more,
        "next_offset": next_offset,
        "remaining": max(total - next_offset, 0),
        "page_size": PAGE_SIZE,
    })


@login_required
def client_dedup_check(request):
    """Лайв-проверка дублей для модалки создания клиента (по мере ввода).

    Телефон — блокирующий сигнал (один номер = один клиент): возвращаем
    конфликт + oob-свап кнопки «Создать» в disabled. ФИО — информационно:
    похожие клиенты по первым буквам (двойники с разными телефонами норм)."""
    last = (request.GET.get("last_name") or "").strip()
    first = (request.GET.get("first_name") or "").strip()
    patr = (request.GET.get("patronymic") or "").strip()
    raw_phone = (request.GET.get("phone") or "").strip()
    # exclude — id клиента, которого исключаем из результатов (форма редактирования:
    # не показываем самого клиента и не считаем его телефон конфликтом).
    exclude = (request.GET.get("exclude") or "").strip()

    # Какой контейнер перерисовывать — по НАЛИЧИЮ параметра в запросе (не по
    # содержимому): иначе на создании очистка телефона не сбросила бы старый
    # баннер. Создание шлёт всю форму (оба есть); редактирование — точечно.
    fio_input = any(k in request.GET for k in ("last_name", "first_name", "patronymic"))
    phone_input = "phone" in request.GET

    def _excl(qs):
        return qs.exclude(pk=exclude) if exclude else qs

    # ФИО — похожие по первым буквам (любое поле от 3 символов), информационно.
    fio_matches = []
    if len(last) >= 3 or len(first) >= 3 or len(patr) >= 3:
        qs = Client.objects.all()
        if last:
            qs = qs.filter(last_name__istartswith=last)
        if first:
            qs = qs.filter(first_name__istartswith=first)
        if patr:
            qs = qs.filter(patronymic__istartswith=patr)
        fio_matches = list(_excl(qs).order_by("last_name", "first_name")[:8])

    # Телефон: от 3 цифр — частичные совпадения (информационно); полный валидный
    # номер, совпавший с ДРУГИМ клиентом — конфликт (блок кнопки «Создать»).
    digits = "".join(ch for ch in raw_phone if ch.isdigit())
    phone_conflict = None
    phone_matches = []
    if len(digits) >= 3:
        norm = normalize_phone(raw_phone)
        if norm:
            cp_qs = ClientPhone.objects.filter(phone=norm, is_active=True).select_related("client")
            if exclude:
                cp_qs = cp_qs.exclude(client_id=exclude)
            cp = cp_qs.first()
            phone_conflict = cp.client if cp else \
                _excl(Client.objects.filter(phone__contains=norm[1:])).first()
        if phone_conflict is None:
            core = digits[-7:] if len(digits) > 7 else digits
            phone_matches = list(
                _excl(Client.objects.filter(phone__contains=core)
                      .exclude(phone="").exclude(phone__isnull=True))
                .order_by("last_name", "first_name")[:8]
            )

    return render(request, "crm/partials/client_dedup_check.html", {
        "fio_input": fio_input,
        "phone_input": phone_input,
        "fio_matches": fio_matches,
        "phone_conflict": phone_conflict,
        "phone_matches": phone_matches,
        "show_submit": not exclude,
    })


@login_required
def client_create(request):
    if request.method == "POST":
        form = ClientForm(request.POST)
        if form.is_valid():
            client = form.save()
            # Форма пишет только Client.phone (кэш) — заводим ClientPhone(primary),
            # чтобы дедуп по телефону работал и для вручную созданных клиентов.
            if client.phone:
                add_client_phone(client, client.phone, "primary")

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
        return render(request, "crm/partials/client_create_modal.html", {"form": form})

    form = ClientForm()
    return render(request, "crm/partials/client_create_modal.html", {"form": form})


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
def employee_report(request):
    """Помесячный табель сотрудника по EmployeeLog (login/logout).

    Рабочее окно 09:00–18:00 пн-пт, обед 13:00–14:00 (1 час). Норма
    8 часов в день. Опоздание — день где первый login позже 09:00 или
    последний logout раньше 18:00.
    """
    from datetime import date, datetime, time, timedelta
    from calendar import monthrange
    from collections import defaultdict

    today = timezone.localdate()
    try:
        year = int(request.GET.get("year") or today.year)
        month = int(request.GET.get("month") or today.month)
    except (TypeError, ValueError):
        year, month = today.year, today.month
    emp_id = (request.GET.get("employee") or "").strip()

    employees_all = (
        Employee.objects.filter(is_active=True)
        .select_related("user").order_by("user__last_name", "user__first_name")
    )
    selected_emp = None
    if emp_id:
        selected_emp = Employee.objects.filter(pk=emp_id).select_related("user").first()

    # Месяц как диапазон.
    last_day = monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)

    days_data = []
    week_totals = defaultdict(timedelta)  # iso week → timedelta
    month_total = timedelta()
    norm_per_day = timedelta(hours=8)
    work_days = 0
    late_days = 0
    early_leave_days = 0
    weekend_work_days = 0

    if selected_emp:
        # Берём все login/logout за месяц.
        logs = list(
            EmployeeLog.objects.filter(
                employee=selected_emp,
                action__in=("login", "logout"),
                timestamp__date__gte=month_start,
                timestamp__date__lte=month_end,
            ).order_by("timestamp")
        )
        # Группируем по дате.
        by_day = defaultdict(list)
        for log in logs:
            by_day[timezone.localtime(log.timestamp).date()].append(log)

        cur = month_start
        while cur <= month_end:
            day_logs = by_day.get(cur, [])
            first_login = None
            last_logout = None
            worked = timedelta()
            for log in day_logs:
                ts_local = timezone.localtime(log.timestamp)
                if log.action == "login":
                    if first_login is None:
                        first_login = ts_local
                elif log.action == "logout":
                    last_logout = ts_local
            if first_login and last_logout:
                start = first_login.time()
                end = last_logout.time()
                # Усекаем окно до рабочего интервала.
                eff_start = max(start, time(9, 0))
                eff_end = min(end, time(18, 0))
                if eff_end > eff_start:
                    seconds = (
                        datetime.combine(date.min, eff_end)
                        - datetime.combine(date.min, eff_start)
                    ).total_seconds()
                    # Вычитаем обед 13-14 если окно покрывает.
                    if eff_start <= time(13, 0) and eff_end >= time(14, 0):
                        seconds -= 3600
                    worked = timedelta(seconds=max(0, seconds))
            iso_week = cur.isocalendar().week
            week_totals[iso_week] += worked
            month_total += worked
            is_weekend = cur.weekday() >= 5
            is_late = bool(first_login and first_login.time() > time(9, 0))
            is_early_leave = bool(last_logout and last_logout.time() < time(18, 0))
            if not is_weekend and worked > timedelta(0):
                work_days += 1
                if is_late:
                    late_days += 1
                if is_early_leave:
                    early_leave_days += 1
            if is_weekend and worked > timedelta(0):
                weekend_work_days += 1
            days_data.append({
                "date": cur,
                "weekday": cur.weekday(),
                "is_weekend": is_weekend,
                "first_login": first_login,
                "last_logout": last_logout,
                "worked": worked,
                "is_late": is_late and not is_weekend,
                "is_early_leave": is_early_leave and not is_weekend,
                "iso_week": iso_week,
            })
            cur += timedelta(days=1)

    def _td_h(td):
        total = td.total_seconds() / 3600
        return round(total, 2)

    # Норма за месяц (пн-пт × 8ч).
    norm_total = timedelta(hours=0)
    cur = month_start
    while cur <= month_end:
        if cur.weekday() < 5:
            norm_total += norm_per_day
        cur += timedelta(days=1)

    week_rows = sorted(week_totals.items())

    return render(request, "crm/logs/report.html", {
        "year": year, "month": month,
        "employees_all": employees_all,
        "selected_emp": selected_emp,
        "emp_id": emp_id,
        "days_data": days_data,
        "week_rows": [(w, _td_h(t)) for w, t in week_rows],
        "month_total_h": _td_h(month_total),
        "norm_total_h": _td_h(norm_total),
        "work_days": work_days,
        "late_days": late_days,
        "early_leave_days": early_leave_days,
        "weekend_work_days": weekend_work_days,
        "td_h": _td_h,
        "year_choices": range(today.year - 3, today.year + 1),
        "month_choices": [
            (1, "Январь"), (2, "Февраль"), (3, "Март"), (4, "Апрель"),
            (5, "Май"), (6, "Июнь"), (7, "Июль"), (8, "Август"),
            (9, "Сентябрь"), (10, "Октябрь"), (11, "Ноябрь"), (12, "Декабрь"),
        ],
    })


@login_required
def logs_list(request):
    from datetime import timedelta
    search = request.GET.get("search", "").strip()
    emp_id = (request.GET.get("employee") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    # Дефолт «последние 7 дней» — иначе COUNT(*) по огромной EmployeeLog
    # делает страницу очень медленной. Юзер может поменять период вручную.
    if not (search or emp_id or date_from or date_to):
        date_from = (timezone.now().date() - timedelta(days=7)).isoformat()

    qs = EmployeeLog.objects.select_related("employee__user", "client", "message")

    if search:
        qs = qs.filter(
            models.Q(employee__user__first_name__icontains=search)
            | models.Q(employee__user__last_name__icontains=search)
            | models.Q(action__icontains=search)
            | models.Q(description__icontains=search)
            | models.Q(client__first_name__icontains=search)
            | models.Q(client__last_name__icontains=search)
        )
    if emp_id:
        qs = qs.filter(employee_id=emp_id)
    if date_from:
        qs = qs.filter(timestamp__date__gte=date_from)
    if date_to:
        qs = qs.filter(timestamp__date__lte=date_to)

    paginator = Paginator(qs.order_by("-timestamp"), 50)
    page_number = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_number)

    # partial=1 → только таблица (для HTMX-filter-form, target=#logs-table).
    # Иначе — full list.html с формой фильтров (открывается через меню HTMX
    # в #content-area).
    template = (
        "crm/logs/list_partial.html"
        if request.GET.get("partial") == "1"
        else "crm/logs/list.html"
    )

    return render(request, template, {
        "page_obj": page_obj,
        "search": search,
        "emp_id": emp_id,
        "date_from": date_from,
        "date_to": date_to,
        "employees_all": Employee.objects.filter(is_active=True)
            .select_related("user").order_by("user__last_name", "user__first_name"),
    })


@login_required
def clients_list(request):
    search = request.GET.get("search", "").strip()

    qs = Client.objects.visible_to(request.user).prefetch_related("employees")

    if search:
        qs = qs.filter(
            models.Q(first_name__icontains=search)
            | models.Q(last_name__icontains=search)
            | models.Q(username__icontains=search)
            | models.Q(phone__icontains=search)
            | models.Q(phones__phone__icontains=search)
            | models.Q(email__icontains=search)
        ).distinct()

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


def _online_employees():
    """Сотрудники, реально присутствующие в системе СЕЙЧАС — по heartbeat-маркеру
    в Redis (`online_emp:<id>`, TTL 150с, ставит idle-poll из открытых вкладок).
    Надёжнее флага Employee.is_online (тот застревает после рестарта/закрытия
    вкладки). Возвращает список Employee, отсортированный по ФИО."""
    emps = list(
        Employee.objects.filter(is_active=True)
        .select_related("user", "department")
        .order_by("user__last_name", "user__first_name")
    )
    keymap = {e.id: f"online_emp:{e.id}" for e in emps}
    present = cache.get_many(list(keymap.values()))
    now_ts = timezone.now().timestamp()
    online = []
    for e in emps:
        ts = present.get(keymap[e.id])
        if ts:
            e.seen_secs_ago = max(0, int(now_ts - float(ts)))
            online.append(e)
    return online


@login_required
def employees_online_count(request):
    return HttpResponse(len(_online_employees()))


@login_required
def employees_online_list(request):
    """Кто сейчас в системе — для попапа по клику на виджет «Активных сотрудников»."""
    online = _online_employees()
    return render(request, "crm/partials/employees_online_list.html",
                  {"online": online, "count": len(online)})


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
@rules_permission_required(
    "crm.edit_client",
    fn=lambda request, client_id: get_object_or_404(Client, pk=client_id),
    raise_exception=True,
)
def client_edit(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    if request.method == "POST":
        form = ClientForm(request.POST, instance=client)
        if form.is_valid():
            form.save()
            if request.headers.get("HX-Request"):
                # Раньше тут был window.location.reload() — он сбрасывал SPA на
                # дефолтный вид дашборда (канбан клиентов), поэтому при
                # сохранении клиента с канбана услуг/моего юзера выкидывало на
                # главный канбан. Вместо перезагрузки закрываем модалку и
                # обновляем ТЕКУЩИЙ канбан на месте через kanbanRefresh
                # (его слушают все три канбана) + serviceChanged.
                return HttpResponse(
                    "<script>(function(){"
                    "var m=document.getElementById('client-edit-modal');"
                    "if(m){try{m.close();}catch(e){}m.remove();}"
                    "document.body.dispatchEvent(new CustomEvent('kanbanRefresh'));"
                    "document.body.dispatchEvent(new CustomEvent('serviceChanged'));"
                    "})();</script>"
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


# ─── CRUD телефонов клиента (ClientPhone) ────────────────────


from .models import ClientPhone  # noqa: E402
from .phone_utils import add_client_phone, normalize_phone, sync_client_phone_cache, find_client_by_phone  # noqa: E402


def _render_phones_block(request, client):
    return render(request, "crm/partials/client_phones_block.html", {
        "client": client,
        "phones": client.phones.order_by("purpose", "phone"),
        "purpose_choices": ClientPhone.PURPOSE_CHOICES,
    })


@login_required
def client_phones_block(request, client_id):
    """HTMX-партиал со списком телефонов клиента + формой добавления."""
    client = get_object_or_404(Client, pk=client_id)
    return _render_phones_block(request, client)


@login_required
def client_phone_add(request, client_id):
    """POST: добавить новый телефон клиента (или вернуть ошибку)."""
    client = get_object_or_404(Client, pk=client_id)
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")
    raw = (request.POST.get("phone") or "").strip()
    purpose = (request.POST.get("purpose") or "additional").strip()
    if purpose not in dict(ClientPhone.PURPOSE_CHOICES):
        return HttpResponseBadRequest("bad purpose")
    phone = normalize_phone(raw)
    if not phone:
        return render(request, "crm/partials/client_phones_block.html", {
            "client": client,
            "phones": client.phones.order_by("purpose", "phone"),
            "purpose_choices": ClientPhone.PURPOSE_CHOICES,
            "error": f"Неверный номер: {raw}",
        })
    # Дедуп: номер не должен принадлежать ДРУГОМУ клиенту ни в каком назначении.
    other = find_client_by_phone(phone)
    if other is not None and other.pk != client.pk:
        fio = f"{other.last_name} {other.first_name}".strip() or "без ФИО"
        return render(request, "crm/partials/client_phones_block.html", {
            "client": client,
            "phones": client.phones.order_by("purpose", "phone"),
            "purpose_choices": ClientPhone.PURPOSE_CHOICES,
            "error": f"+{phone} уже у клиента «{fio}» — дубликат номера запрещён.",
        })
    obj = add_client_phone(client, phone, purpose)
    if obj is None:
        return render(request, "crm/partials/client_phones_block.html", {
            "client": client,
            "phones": client.phones.order_by("purpose", "phone"),
            "purpose_choices": ClientPhone.PURPOSE_CHOICES,
            "error": f"+{phone} уже привязан к другому клиенту в назначении "
                     f"«{dict(ClientPhone.PURPOSE_CHOICES).get(purpose)}»",
        })
    sync_client_phone_cache(client)
    return _render_phones_block(request, client)


@login_required
def client_phone_delete(request, phone_id):
    """POST: удалить телефон у клиента."""
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")
    cp = get_object_or_404(ClientPhone, pk=phone_id)
    client = cp.client
    cp.delete()
    sync_client_phone_cache(client)
    return _render_phones_block(request, client)


@login_required
def client_phone_set_purpose(request, phone_id):
    """POST: сменить назначение существующего телефона."""
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")
    cp = get_object_or_404(ClientPhone, pk=phone_id)
    purpose = (request.POST.get("purpose") or "").strip()
    if purpose not in dict(ClientPhone.PURPOSE_CHOICES):
        return HttpResponseBadRequest("bad purpose")
    # Конфликт: если такой phone уже занят в новом назначении.
    conflict = ClientPhone.objects.filter(
        phone=cp.phone, purpose=purpose,
    ).exclude(pk=cp.pk).first()
    if conflict:
        client = cp.client
        return render(request, "crm/partials/client_phones_block.html", {
            "client": client,
            "phones": client.phones.order_by("purpose", "phone"),
            "purpose_choices": ClientPhone.PURPOSE_CHOICES,
            "error": f"+{cp.phone} в назначении «{dict(ClientPhone.PURPOSE_CHOICES).get(purpose)}» уже занят",
        })
    cp.purpose = purpose
    cp.save(update_fields=["purpose", "updated_at"])
    sync_client_phone_cache(cp.client)
    return _render_phones_block(request, cp.client)


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

    # Мультислово: «Каныгин Денис» → каждое слово должно match'иться
    # хоть в одном из полей.
    clients_qs = Client.objects.all()
    for word in q.split():
        clients_qs = clients_qs.filter(
            Q(first_name__icontains=word) | Q(last_name__icontains=word) |
            Q(patronymic__icontains=word) | Q(username__icontains=word) |
            Q(phone__icontains=word) | Q(phones__phone__icontains=word)
        )
    clients = clients_qs.distinct().prefetch_related(
        "services__name", "services__common_status", "services__region",
    ).order_by("last_name", "first_name")[:12]

    legal_entities = LegalEntity.objects.filter(
        Q(name__icontains=q) | Q(inn__icontains=q) | Q(ogrn__icontains=q)
    ).order_by("name")[:12]

    messages = Message.objects.filter(
        content__icontains=q
    ).select_related("client").order_by("-created_at")[:12]

    # Файлы клиентов — по ClientFile.name (мультислово AND).
    from apps.files.models import ClientFile
    files_qs = ClientFile.objects.all()
    for word in q.split():
        files_qs = files_qs.filter(name__icontains=word)
    files = files_qs.select_related(
        "folder__client", "stored_file"
    ).order_by("-created_at")[:12]

    # Доступ к клиентам (object-level visibility). Затем затеняем строки
    # тех клиентов, которые есть в результатах поиска, но недоступны.
    visible_ids = set(
        Client.objects.visible_to(request.user)
        .values_list("id", flat=True)
    )
    for c in clients:
        c.no_access = c.id not in visible_ids
    for m in messages:
        m.no_access = m.client_id not in visible_ids
    for f in files:
        f.no_access = f.folder.client_id not in visible_ids

    empty = not (clients or legal_entities or messages or files)
    return render(request, "crm/partials/global_search_results.html", {
        "q": q, "clients": clients, "legal_entities": legal_entities,
        "messages": messages, "files": files, "empty": empty,
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
    """Услуги, видимые пользователю. Логика — в ``Service.objects.visible_to``."""
    return Service.objects.visible_to(user).select_related(
        "client", "agent", "name", "region", "common_status", "payment_procedure",
    ).prefetch_related(
        "employee_states__employee__user", "employee_states__status",
        "tag_assignments__tag", "tag_assignments__employee",
    )


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

    # Контроль доступа: нельзя редактировать чужую услугу. Для pk=None
    # (создание новой) объектной проверки нет — see apps.crm.rules.
    if svc and not request.user.has_perm("crm.edit_service", svc):
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
                    # action 'service_create' автоматически породит event 'service_created'.
                    client_log.record_action(
                        svc_new.client, "service_create",
                        comment=f"Добавлена услуга: {svc_label}",
                        new_value=svc_new.name.short_name,
                        employee=emp,
                    )
                    if assigned:
                        names = ", ".join(a.user.get_full_name() or a.user.username for a in assigned)
                        client_log.record_event(
                            svc_new.client, "employee_assigned",
                            comment=f"Услуга {svc_label}: назначены исполнители — {names}",
                            employee=emp,
                        )

            # Сохраняем личный статус сотрудника.
            # 🛑 Меняем ТОЛЬКО когда emp_status явно выбран (непустой и валидный).
            # Пустое значение приходит, когда выпадашка «Мой статус» отфильтровалась
            # в пусто (личный статус услуги не совпал с выбранным общим статусом —
            # частый рассинхрон после передач). Раньше это МОЛЧА обнуляло личный
            # статус → услуга пропадала из «Моего канбана» при любом сохранении
            # (в т.ч. при оформлении договора). Пустое = «не трогать».
            emp_status_id = request.POST.get("emp_status")
            if emp and svc_new.pk and emp_status_id:
                new_status = ServiceEmployeeStatus.objects.filter(
                    pk=emp_status_id, employee=emp).first()
                if new_status:
                    state, _ = ServiceEmployeeState.objects.get_or_create(
                        service=svc_new, employee=emp,
                    )
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
@rules_permission_required(
    "crm.delete_service",
    fn=lambda request, pk: get_object_or_404(Service, pk=pk),
    raise_exception=True,
)
def service_delete(request, pk):
    emp = _current_employee_from_user(request.user)
    svc = get_object_or_404(Service, pk=pk)
    client_id  = svc.client_id
    svc_label  = svc.numb_dogovor or svc.name.short_name
    svc_name   = svc.name.short_name
    svc.delete()
    if client_id:
        client_for_log = Client.objects.filter(pk=client_id).first()
        if client_for_log is not None:
            # action 'service_delete' автоматически породит event 'service_deleted'.
            client_log.record_action(
                client_for_log, "service_delete",
                comment=f"Удалена услуга: {svc_label}",
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
                | Q(phones__phone__icontains=q)
                | Q(username__icontains=q)
            ).distinct().order_by("last_name", "first_name")[:15]
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
    q           = (request.GET.get("q") or "").strip()
    qs = _visible_services_qs(request.user).filter(common_status=status)
    if employee_id:
        qs = qs.filter(employees__id=employee_id)
    # Поиск из верхнего поля (#kanban-filter-form q) — по ФИО/телефону клиента.
    if q:
        for word in q.split():
            qs = qs.filter(
                Q(client__first_name__icontains=word)
                | Q(client__last_name__icontains=word)
                | Q(client__patronymic__icontains=word)
                | Q(client__phone__icontains=word)
                | Q(client__phones__phone__icontains=word)
            )
        qs = qs.distinct()
    # Считаем total отдельным count() и режем срезом — иначе при большом
    # количестве услуг каждая колонка тянет всё в память (5-10 колонок
    # параллельно × сотни услуг с prefetch — отсюда «постоянно грузится»).
    PAGE_SIZE = 30
    total = qs.count()
    services = list(qs.order_by("-created_at")[:PAGE_SIZE])
    return render(request, "crm/partials/kanban_services_column.html", {
        "services": services, "status": status,
        "for_my_kanban": False, "count": total,
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
        svc_label = service.numb_dogovor or service.name.short_name
        client_log.record_event(
            service.client, "status_change",
            comment=(
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
    CAN_VIEW_OTHERS = is_management(request.user)

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
        # is_inbox=True (инбокс) — первым; затем по услуге/стадии.
        .order_by("-is_inbox", "common_status__service_name__short_name", "common_status__order", "order")
    )

    groups = []
    for cs, statuses_iter in _groupby(emp_statuses, key=lambda s: s.common_status_id):
        statuses_list = list(statuses_iter)
        first = statuses_list[0]
        groups.append({
            "common_status": first.common_status,   # None для инбокса
            "is_inbox": first.is_inbox,
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
    CAN_VIEW_OTHERS = is_management(request.user)
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
    # Поиск из верхнего поля (#kanban-filter-form q) — по ФИО/телефону клиента.
    q = (request.GET.get("q") or "").strip()
    if q:
        for word in q.split():
            qs = qs.filter(
                Q(client__first_name__icontains=word)
                | Q(client__last_name__icontains=word)
                | Q(client__patronymic__icontains=word)
                | Q(client__phone__icontains=word)
                | Q(client__phones__phone__icontains=word)
            )
        qs = qs.distinct()
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
    """Drag-and-drop в Моём канбане: смена личного статуса услуги.

    Руководитель (is_management), просматривающий чужой «Мой канбан»
    (?viewed_employee=), двигает карточки ТОГО сотрудника — поэтому статус и
    состояние резолвим по просматриваемому сотруднику. Иначе колонки несут
    ServiceEmployeeStatus просматриваемого, а lookup шёл по текущему юзеру →
    get_object_or_404 не находил чужой статус → 404.
    """
    current_emp = _current_employee_from_user(request.user)
    if not current_emp:
        return HttpResponse("", status=403)

    viewed_emp_id = request.POST.get("viewed_employee") or ""
    if is_management(request.user) and viewed_emp_id:
        emp = get_object_or_404(Employee, pk=viewed_emp_id, is_active=True)
    else:
        emp = current_emp

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

    # «Принятие»: если услугу вытащили ИЗ инбокса «Не принято» в рабочую
    # колонку — сотрудник взял её в работу. Убираем услугу из инбоксов
    # остальных сотрудников отдела (один взял — у других пропала).
    if (old_status and getattr(old_status, "is_inbox", False)
            and not getattr(new_status, "is_inbox", False)):
        others = ServiceEmployeeState.objects.filter(
            service=service, status__is_inbox=True,
        ).exclude(employee=emp)
        other_ids = list(others.values_list("employee_id", flat=True))
        if other_ids:
            others.delete()
            service.employees.remove(*other_ids)

    ServiceLog.objects.create(
        service=service,
        employee=emp,
        action="status_change",
        old_status=old_status,
        new_status=new_status,
    )

    if service.client_id:
        svc_label = service.numb_dogovor or service.name.short_name
        client_log.record_event(
            service.client, "status_change",
            comment=(
                f"Услуга {svc_label}: мой статус изменён "
                f"«{old_status.name if old_status else '—'}» → «{new_status.name}»"
            ),
            old_value=old_status.name if old_status else "",
            new_value=new_status.name,
            employee=emp,
        )
    return HttpResponse(status=204)


@login_required
def service_transfer_modal(request, pk):
    """Пикер «Передать в работу отдела/сотрудника» для услуги."""
    from apps.crm.service_transfer import eligible_employees
    service = get_object_or_404(
        Service.objects.select_related("name", "client", "common_status__department"), pk=pk,
    )
    # Получатели: действующие сотрудники, работающие с этой услугой.
    emp_qs = eligible_employees(service).select_related("user", "department").order_by(
        "department__name", "user__last_name", "user__first_name",
    )
    employees = list(emp_qs)
    # «В отдел» — отделы, в которых есть такие сотрудники.
    dept_ids = {e.department_id for e in employees if e.department_id}
    departments = Department.objects.filter(
        id__in=dept_ids, is_active=True,
    ).order_by("name")
    return render(request, "crm/partials/service_transfer_modal.html", {
        "service": service,
        "departments": departments,
        "employees": employees,
    })


@login_required
@require_POST
def service_transfer(request, pk):
    """Выполнить передачу услуги в отдел/сотруднику."""
    from apps.crm.service_transfer import transfer_service
    service = get_object_or_404(Service, pk=pk)
    actor = _current_employee_from_user(request.user)

    target_type = (request.POST.get("target_type") or "").strip()
    target_id = (request.POST.get("target_id") or "").strip()
    if not target_id:
        return HttpResponseBadRequest("Не выбран получатель")

    # «У меня завершить» (галочка по умолчанию стоит): finish_self=True →
    # keep_actor=False (полный переезд). Снята → услуга остаётся у актора.
    finish_self = request.POST.get("finish_self") == "1"
    keep_actor = not finish_self
    comment = (request.POST.get("comment") or "").strip()

    try:
        if target_type == "employee":
            emp = get_object_or_404(Employee, pk=target_id, is_active=True)
            transfer_service(service, target_employee=emp, actor=actor,
                             keep_actor=keep_actor, comment=comment)
        elif target_type == "dept":
            dept = get_object_or_404(Department, pk=target_id, is_active=True)
            transfer_service(service, target_department=dept, actor=actor,
                             keep_actor=keep_actor, comment=comment)
        else:
            return HttpResponseBadRequest("Неверный тип получателя")
    except ValueError as e:
        return HttpResponse(str(e), status=400)

    # Перерисовываем модалку услуги (как GET) + сигнал на обновление канбанов.
    from django.http import QueryDict
    request.method = "GET"
    request.POST = QueryDict()
    resp = service_edit(request, pk)
    resp["HX-Trigger"] = "serviceChanged"
    return resp


@login_required
def client_events_modal(request, client_id):
    """Модалка лога клиента: события + действия в одной хронологии.

    Фильтры (GET):
      ?kind=event|action|''  (пусто = все)
      ?source=system|court|client|legal_entity|employee  (только для events)
      ?type=<code>           (код EventType ИЛИ ActionType, в зависимости от kind)
      ?q=<строка>            (поиск в comment)
    """
    client = get_object_or_404(
        Client.objects.prefetch_related("employees__user"), pk=client_id,
    )

    f_kind   = (request.GET.get("kind") or "").strip()
    f_source = (request.GET.get("source") or "").strip()
    f_type   = (request.GET.get("type") or "").strip()
    f_q      = (request.GET.get("q") or "").strip()

    qs = ClientLogEntry.objects.filter(client=client).select_related(
        "employee__user", "event_type", "action_type", "stored_file",
        "parent__event_type", "parent__action_type",
    ).prefetch_related("event_type__standard_actions")

    if f_kind in ("event", "action"):
        qs = qs.filter(kind=f_kind)
    if f_source:
        if f_source == "employee":
            # «Сотрудник» = всё, что сделал сотрудник: ДЕЙСТВИЯ (у них нет
            # поля source — они по определению совершаются сотрудником) +
            # события с источником employee (если такие появятся).
            qs = qs.filter(
                Q(kind="action") | Q(kind="event", event_type__source="employee")
            )
        else:
            # Остальные источники имеют смысл только для событий.
            qs = qs.filter(kind="event", event_type__source=f_source)
    if f_type:
        if f_kind == "action":
            qs = qs.filter(action_type__code=f_type)
        elif f_kind == "event":
            qs = qs.filter(event_type__code=f_type)
        else:
            qs = qs.filter(
                Q(event_type__code=f_type) | Q(action_type__code=f_type)
            )
    if f_q:
        qs = qs.filter(comment__icontains=f_q)

    qs = qs.order_by("created_at")  # хронологически, как в чатах: новое снизу

    event_types = EventType.objects.filter(is_active=True).order_by(
        "source", "order", "name",
    )
    action_types = ActionType.objects.filter(is_active=True).order_by(
        "order", "name",
    )
    # Для формы ручного добавления — только типы с is_manual (авто-генерируемые
    # скрыты). Фильтр сверху по-прежнему работает по всем типам (event_types/
    # action_types) — чтобы можно было отфильтровать уже записанные авто-события.
    manual_event_types = event_types.filter(is_manual=True)
    manual_action_types = action_types.filter(is_manual=True)

    return render(request, "crm/partials/client_events_modal.html", {
        "client": client,
        "events": qs,
        "event_types": event_types,
        "action_types": action_types,
        "manual_event_types": manual_event_types,
        "manual_action_types": manual_action_types,
        "source_choices": EventType.SOURCE_CHOICES,
        "filter_kind": f_kind,
        "filter_source": f_source,
        "filter_type": f_type,
        "filter_q": f_q,
    })


@login_required
@require_POST
def client_log_add(request, client_id):
    """POST из формы добавления в модалке лога. Создаёт ClientLogEntry и
    возвращает ТОЛЬКО HTML новой записи (action + порождённое событие, если
    есть) — фронт добавляет их в конец ленты (hx-swap=beforeend) без ребилда
    модалки, чтобы она не моргала и не прыгала.

    Поля формы:
      entry_kind  — 'event' | 'action' (что создаём)
      type_code   — code EventType/ActionType
      comment     — текст
      parent_id   — uuid родительской записи (опц.)
    """
    client = get_object_or_404(Client, pk=client_id)
    entry_kind = (request.POST.get("entry_kind") or "").strip()
    type_code  = (request.POST.get("type_code") or "").strip()
    comment    = (request.POST.get("comment") or "").strip()
    parent_id  = (request.POST.get("parent_id") or "").strip() or None

    if entry_kind not in ("event", "action") or not type_code:
        return HttpResponseBadRequest("entry_kind и type_code обязательны")

    try:
        emp = Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        emp = None

    parent = None
    if parent_id:
        parent = ClientLogEntry.objects.filter(pk=parent_id, client=client).first()

    if entry_kind == "event":
        entry = client_log.record_event(client, type_code, comment=comment, employee=emp, parent=parent)
    else:
        entry = client_log.record_action(client, type_code, comment=comment, employee=emp, parent=parent)

    if entry is None:
        # тип не найден в справочнике (см. client_log) — не падаем
        return HttpResponseBadRequest("Неизвестный тип записи")

    # Новые записи: сама запись + порождённое событие (action.spawns_event),
    # если оно было создано. Перечитываем с select_related для рендера строки.
    created_ids = [entry.id] + list(
        ClientLogEntry.objects.filter(parent=entry).values_list("id", flat=True)
    )
    rows = ClientLogEntry.objects.filter(id__in=created_ids).select_related(
        "employee__user", "event_type", "action_type", "stored_file",
        "parent__event_type", "parent__action_type",
    ).prefetch_related("event_type__standard_actions").order_by("created_at")

    html = "".join(
        render_to_string("crm/partials/_log_entry_row.html", {"ev": ev}, request=request)
        for ev in rows
    )
    return HttpResponse(html)


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
            if phone:
                from apps.crm.phone_utils import add_client_phone
                add_client_phone(client, phone, purpose="primary")

            try:
                actor = Employee.objects.get(user=request.user)
            except Employee.DoesNotExist:
                actor = None

            new_repr = (
                f"{last_name} {first_name} {patronymic}".strip()
                + (f", тел. {phone}" if phone else "")
            )
            client_log.record_action(
                client, "client_identified",
                comment=f"Идентифицирован. Было: «{old_repr}» → стало: «{new_repr}».",
                old_value=old_repr,
                new_value=new_repr,
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

    # Смена (а не добавление «+»): убираем ИМЕННО прежнего ответственного, по
    # которому кликнули. Иначе ответственные накапливались (клиент висел сразу
    # на нескольких), а карточка показывала случайного из них.
    if prev_employee and prev_employee != new_employee:
        ClientEmployee.objects.filter(client=client, employee=prev_employee).delete()

    # Лог события только если назначение реально изменилось
    if created or (prev_employee and prev_employee != new_employee):
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

        client_log.record_event(
            client, "employee_assigned", comment=desc, employee=actor,
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

    client_log.record_event(
        client, "status_change",
        comment=(
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
            event_code = "employee_assigned"
        else:
            desc = f"Услуга {svc_label}: снят исполнитель — {emp_name}"
            event_code = "employee_removed"
        client_log.record_event(
            service.client, event_code, comment=desc, employee=actor,
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


# ─────────────────────────────────────────────────────────────────────────
# Объединение карточек-дублей (право can_merge_clients)
# ─────────────────────────────────────────────────────────────────────────
from apps.core.permissions import can_merge_clients as _can_merge_clients  # noqa: E402
from apps.crm import client_merge as _cm  # noqa: E402


def _merge_guard(request):
    return _can_merge_clients(request.user)


@login_required
def client_merge_modal(request, client_id):
    """Шаг 1: модалка объединения — поиск второй карточки."""
    if not _merge_guard(request):
        return HttpResponseForbidden("Нет доступа к объединению клиентов")
    client = get_object_or_404(Client, pk=client_id)
    return render(request, "crm/partials/client_merge_modal.html", {"client": client})


@login_required
def client_merge_search(request, client_id):
    """Кандидаты для объединения (поиск по ФИО/телефону), кроме самой карточки."""
    if not _merge_guard(request):
        return HttpResponseForbidden("Нет доступа")
    q = (request.GET.get("q") or "").strip()
    results = []
    if len(q) >= 3:
        digits = "".join(ch for ch in q if ch.isdigit())
        qs = Client.objects.exclude(pk=client_id)
        if digits and len(digits) >= 3:
            core = digits[-7:] if len(digits) > 7 else digits
            ids = set(ClientPhone.objects.filter(phone__contains=core)
                      .values_list("client_id", flat=True))
            qs = qs.filter(Q(pk__in=ids) | Q(phone__contains=core))
        else:
            parts = q.split()
            for p in parts:
                qs = qs.filter(Q(last_name__icontains=p) | Q(first_name__icontains=p)
                               | Q(patronymic__icontains=p))
        results = list(qs.order_by("last_name", "first_name")[:12])
    return render(request, "crm/partials/client_merge_candidates.html", {
        "client_id": client_id, "results": results, "q": q,
    })


@login_required
def client_merge_compare(request, client_id):
    """Шаг 2: таблица сравнения Клиент1 (текущий) ↔ Клиент2 (выбранный)."""
    if not _merge_guard(request):
        return HttpResponseForbidden("Нет доступа")
    c1 = get_object_or_404(Client, pk=client_id)
    other_id = (request.GET.get("other") or "").strip()
    c2 = get_object_or_404(Client, pk=other_id)
    if str(c1.id) == str(c2.id):
        return HttpResponseBadRequest("Нельзя объединить карточку саму с собой")
    data = _cm.compare_clients(c1, c2)
    return render(request, "crm/partials/client_merge_compare.html", {
        "c1": c1, "c2": c2,
        "scalars": data["scalars"],
        "collections": data["collections"],
        "dup_services": data["dup_services"],
    })


@login_required
@require_POST
def client_merge_execute(request, client_id):
    """Выполнить объединение по выбору пользователя."""
    if not _merge_guard(request):
        return HttpResponseForbidden("Нет доступа")
    c1 = get_object_or_404(Client, pk=client_id)
    c2 = get_object_or_404(Client, pk=(request.POST.get("other") or "").strip())
    if str(c1.id) == str(c2.id):
        return HttpResponseBadRequest("Нельзя объединить карточку саму с собой")

    # Кто выживает: 'c1' (по умолчанию) или 'c2'
    keep = request.POST.get("survivor") or "c1"
    survivor, other = (c1, c2) if keep == "c1" else (c2, c1)

    # Одиночные поля: для каждого поля выбран 'c1'|'c2'. Берём значение OTHER,
    # если выбран столбец, противоположный survivor'у.
    survivor_col = keep  # 'c1' или 'c2'
    scalar_take_other = set()
    for name, _ in _cm.SCALAR_FIELDS:
        choice = request.POST.get(f"f_{name}") or "c1"
        if choice != survivor_col:
            scalar_take_other.add(name)

    # Коллекции: выбор 'c1'|'c2'|'both' → 'survivor'|'other'|'both'
    collection_actions = {}
    for key, _, _ in _cm.COLLECTIONS:
        choice = request.POST.get(f"col_{key}") or "both"
        if choice == "both":
            collection_actions[key] = "both"
        elif choice == survivor_col:
            collection_actions[key] = "survivor"
        else:
            collection_actions[key] = "other"

    try:
        _cm.merge_clients(survivor, other,
                          scalar_take_other=scalar_take_other,
                          collection_actions=collection_actions)
    except Exception as e:
        logger.exception("Ошибка объединения клиентов %s ← %s", survivor.id, other.id)
        return HttpResponse(
            f'<div class="alert alert-error text-sm">Ошибка объединения: {e}</div>',
            status=200)

    fio = f"{survivor.last_name} {survivor.first_name}".strip()
    return HttpResponse(
        "<script>"
        "document.getElementById('client-merge-modal')?.remove();"
        "document.getElementById('client-edit-modal')?.remove();"
        f"window.showToast && showToast('Карточки объединены → {fio}','success');"
        "htmx.trigger(document.body,'kanbanRefresh');"
        "</script>")
