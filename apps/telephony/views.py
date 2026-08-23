"""Раздел «Звонки»: журнал телефонных разговоров и прослушивание записей.

Панель грузится в #content-area пунктом меню (use_htmx), фильтры и пагинация —
целиком в SQL, как в сводной таблице процедур: журнал растёт на тысячи строк,
и вытягивать его в питон нельзя.

🛑 Ссылка на запись выдаётся presigned-URL с коротким сроком жизни и только
через ``call_recording`` — она же пишет строку в ``CallListen``. Прямых ссылок
на S3 в шаблонах нет: иначе прослушивание не попадёт в журнал.
"""
import datetime
import logging
import re
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.core.models import Employee
from apps.core.permissions import get_employee

from .models import Call, CallListen
from .permissions import can_access_calls, require_calls

logger = logging.getLogger(__name__)

PER_PAGE_CHOICES = (25, 50, 100)
DEFAULT_PER_PAGE = 50
# Ссылка на запись живёт ровно столько, сколько нужно, чтобы нажать «play».
RECORDING_URL_TTL = 600

# Ключ сортировки → поля ORDER BY. 🛑 В каждом наборе последним идёт тай-брейк
# по id: без него строки прыгают между страницами при равных значениях
# (та же грабля, что в сводной таблице процедур).
SORT_FIELDS = {
    "started": ("started_at",),
    "direction": ("direction", "started_at"),
    "phone": ("counterparty_phone", "started_at"),
    "client": ("client__last_name", "client__first_name", "started_at"),
    "employee": ("employee__user__last_name", "started_at"),
    "billsec": ("billsec", "started_at"),
    "outcome": ("outcome", "started_at"),
}
DEFAULT_SORT = "started"


def _filtered_calls(request):
    """Журнал с наложенными фильтрами. Всё — в SQL."""
    qs = Call.objects.select_related("client", "employee__user", "recording")

    q = (request.GET.get("q") or "").strip()
    if q:
        cond = Q(src__icontains=q) | Q(dst__icontains=q) | Q(clid__icontains=q)

        # 🛑 Номер лежит в трёх видах сразу: `src`/`dst` — как набрали на АТС
        # (`89537741564`), `counterparty_phone` — нормализованный
        # (`79537741564`). Поэтому «+7953» не найдётся ни подстрокой по dst
        # (там восьмёрка), ни точным сравнением (это не полный номер).
        # Ищем по ЦИФРАМ запроса и отдельно по варианту без ведущей 7/8.
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 3:
            variants = {digits}
            if digits[0] in "78":
                variants.add(digits[1:])
            for v in variants:
                if len(v) >= 3:
                    cond |= (Q(counterparty_phone__contains=v)
                             | Q(src__contains=v) | Q(dst__contains=v))

        # Поиск по ФИО клиента — по словам, как в фильтре канбана
        # (у Client нет единого поля name, только first_name/last_name).
        for word in q.split():
            cond |= Q(client__first_name__icontains=word) | Q(client__last_name__icontains=word)

        qs = qs.filter(cond)

    direction = (request.GET.get("direction") or "").strip()
    if direction in dict(Call.DIRECTION_CHOICES):
        qs = qs.filter(direction=direction)

    emp_id = (request.GET.get("employee") or "").strip()
    if emp_id.isdigit():
        qs = qs.filter(employee_id=int(emp_id))

    if (request.GET.get("with_recording") or "") == "1":
        qs = qs.filter(recording__isnull=False)

    outcome = (request.GET.get("outcome") or "").strip()
    if outcome in dict(Call.OUTCOME_CHOICES):
        qs = qs.filter(outcome=outcome)

    date_from = _parse_date(request.GET.get("date_from"))
    if date_from:
        qs = qs.filter(started_at__gte=timezone.make_aware(
            datetime.datetime.combine(date_from, datetime.time.min)))
    date_to = _parse_date(request.GET.get("date_to"))
    if date_to:
        qs = qs.filter(started_at__lte=timezone.make_aware(
            datetime.datetime.combine(date_to, datetime.time.max)))

    client_id = (request.GET.get("client") or "").strip()
    if client_id:
        qs = qs.filter(client_id=client_id)

    return qs


def _parse_date(raw):
    """<input type="date"> отдаёт ISO. 🛑 Разбираем строго ISO — вольный парсер
    на «21.08.2026» молча даст не тот месяц."""
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _sorted(qs, sort, direction):
    fields = SORT_FIELDS.get(sort) or SORT_FIELDS[DEFAULT_SORT]
    prefix = "" if direction == "asc" else "-"
    order = [f"{prefix}{f}" for f in fields]
    order.append(f"{prefix}id")     # тай-брейк, иначе строки прыгают по страницам
    return qs.order_by(*order)


def _context(request):
    qs = _filtered_calls(request)

    sort = request.GET.get("sort") or DEFAULT_SORT
    if sort not in SORT_FIELDS:
        sort = DEFAULT_SORT
    direction = "asc" if request.GET.get("dir") == "asc" else "desc"

    try:
        per_page = int(request.GET.get("per_page") or DEFAULT_PER_PAGE)
    except (TypeError, ValueError):
        per_page = DEFAULT_PER_PAGE
    if per_page not in PER_PAGE_CHOICES:
        per_page = DEFAULT_PER_PAGE

    paginator = Paginator(_sorted(qs, sort, direction), per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    # Сводка по ВСЕЙ выборке, а не по странице — иначе цифры вводят в заблуждение.
    # Отдельным запросом с агрегатами, без вытягивания строк в питон.
    stats = qs.aggregate(
        answered=Count("id", filter=Q(outcome=Call.OUTCOME_ANSWERED)),
        voicemail=Count("id", filter=Q(outcome=Call.OUTCOME_VOICEMAIL)),
        missed=Count("id", filter=Q(outcome__in=[Call.OUTCOME_MISSED, Call.OUTCOME_NO_ANSWER])),
        with_rec=Count("id", filter=Q(recording__isnull=False)),
        talk=Sum("billsec"),
    )
    talk = stats["talk"] or 0

    f = {k: request.GET.get(k, "") for k in
         ("q", "direction", "employee", "with_recording", "outcome", "date_from", "date_to", "client")}

    return {
        "page_obj": page_obj,
        "paginator": paginator,
        "calls": page_obj.object_list,
        "page_range": list(paginator.get_elided_page_range(
            page_obj.number, on_each_side=1, on_ends=1)),
        "ellipsis": Paginator.ELLIPSIS,
        "total": paginator.count,
        "row_offset": (page_obj.number - 1) * per_page,
        "stat_answered": stats["answered"] or 0,
        "stat_voicemail": stats["voicemail"] or 0,
        "stat_missed": stats["missed"] or 0,
        "outcomes": Call.OUTCOME_CHOICES,
        "stat_with_rec": stats["with_rec"] or 0,
        "stat_talk_human": _human_duration(talk),
        "sort": sort,
        "direction_sort": direction,
        "per_page": per_page,
        "per_page_choices": PER_PAGE_CHOICES,
        "directions": Call.DIRECTION_CHOICES,
        "employees": Employee.objects.filter(user__is_active=True)
                                     .exclude(sip_extension="")
                                     .select_related("user")
                                     .order_by("user__last_name"),
        "filters_active": any(f[k] for k in
                              ("q", "direction", "employee", "with_recording",
                               "outcome", "date_from", "date_to", "client")),
        "f": f,
        "oob": request.headers.get("HX-Request") == "true",
    }


def _human_duration(seconds: int) -> str:
    """4271 → «1 ч 11 мин». Секунды показываем только когда минут нет."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds} с"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


@login_required
@require_calls
@never_cache
def panel(request):
    """Раздел «Звонки».

    🛑 При НЕ-HTMX заходе (прямая ссылка, F5, возврат после логина) редиректим
    на дашборд с ?open=: панель — это партиал для #content-area, сама по себе
    она отдаётся без вёрстки и без htmx, и выглядит как сломанная страница,
    где ничего не работает. Тот же приём, что в devops.action_poll.
    """
    if "HX-Request" not in request.headers:
        params = request.GET.urlencode()
        target = "/telephony/" + (f"?{params}" if params else "")
        return redirect(f"/?open={quote(target, safe='')}")
    return render(request, "telephony/panel.html", _context(request))


@login_required
@require_calls
@never_cache
def call_list(request):
    """Таблица журнала — HTMX-партиал (пагинация и фильтры)."""
    return render(request, "telephony/partials/_call_list.html", _context(request))


@login_required
@require_calls
@never_cache
def call_recording(request, call_id):
    """Отдать временную ссылку на mp3 и записать факт прослушивания.

    🛑 Единственная точка выдачи ссылки — здесь. Журнал прослушиваний должен
    оставаться полным: записи разговоров содержат персональные данные клиентов.
    """
    call = get_object_or_404(Call.objects.select_related("recording"), pk=call_id)
    if not call.recording:
        return JsonResponse({"error": "no_recording"}, status=404)

    from apps.files.s3_utils import get_presigned_url
    try:
        # inline + явный тип: иначе браузер скачает файл вместо проигрывания.
        url = get_presigned_url(call.recording.bucket, call.recording.key,
                                expiration=RECORDING_URL_TTL,
                                inline=True, content_type="audio/mpeg")
    except Exception:
        return JsonResponse({"error": "storage_unavailable"}, status=502)
    if not url:
        return JsonResponse({"error": "storage_unavailable"}, status=502)

    CallListen.objects.create(
        call=call, employee=get_employee(request.user),
        ip=_client_ip(request),
    )
    return JsonResponse({"url": url, "expires_in": RECORDING_URL_TTL})


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")
    return ip or None


@login_required
@never_cache
def client_calls(request, client_id):
    """Звонки конкретного клиента — блок в карточке клиента.

    Виден всем, кто видит клиента: сам факт звонка (когда, кто, сколько длился)
    нужен в работе. А вот кнопка прослушивания появляется только при
    ``can_access_calls`` — её рисует шаблон.
    """
    calls = (Call.objects.filter(client_id=client_id)
             .select_related("employee__user", "recording")
             .order_by("-started_at")[:50])
    return render(request, "telephony/partials/_client_calls.html", {
        "calls": calls,
        "client_id": client_id,
        "can_listen": can_access_calls(request.user),
    })


@login_required
@require_POST
@never_cache
def place_call(request):
    """Звонок по клику. Принимает ``client`` (uuid) или ``phone``.

    🛑 Права отдельного не заводим: звонить может любой сотрудник, у которого
    есть внутренний номер (`Employee.sip_extension`) — это его собственный
    рабочий телефон, а не доступ к чужим данным. Прослушивание записей —
    другое дело, там своё право.
    """
    from .services import place_call as _place

    emp = get_employee(request.user)
    phone = (request.POST.get("phone") or "").strip()
    client_id = (request.POST.get("client") or "").strip()

    client = None
    if client_id:
        from apps.crm.models import Client
        client = Client.objects.filter(pk=client_id).first()
        if client is None:
            return JsonResponse({"ok": False, "error": "Клиент не найден"}, status=404)
        # 🛑 Номер берём тем же правилом, что и остальная CRM
        # (`phone_utils.sync_client_phone_cache`): сперва purpose="primary",
        # затем "additional". У ClientPhone НЕТ поля is_primary — «основной»
        # выражен именно назначением номера.
        if not phone:
            cp = (client.phones.filter(purpose="primary", is_active=True).first()
                  or client.phones.filter(purpose="additional", is_active=True).first()
                  or client.phones.filter(is_active=True).first())
            phone = cp.phone if cp else (client.phone or "")

    if not phone:
        return JsonResponse({"ok": False, "error": "У клиента нет телефона"}, status=400)

    ok, message = _place(emp, phone)
    if ok and client is not None:
        _log_outgoing_attempt(client, emp, phone)
    return JsonResponse({"ok": ok, "message" if ok else "error": message},
                        status=200 if ok else 502)


def _log_outgoing_attempt(client, employee, phone):
    """Отметка в событийке клиента: кто и когда инициировал звонок.

    Сам факт разговора приедет позже с АТС (агент перенесёт CDR), а эта запись
    фиксирует именно действие сотрудника — иначе непонятно, кто звонил, если
    клиент не поднял трубку.
    """
    try:
        from apps.crm.client_log import record_action
        record_action(client, "call_client", employee=employee,
                      comment=f"Исходящий звонок на {phone}")
    except Exception:
        # Событийка не должна ронять звонок.
        logger.debug("не удалось записать действие о звонке", exc_info=True)


# ── Карточки «вам звонили» ────────────────────────────────────────────────
def _today_alerts(employee):
    """Сегодняшние неубранные карточки сотрудника.

    🛑 Ограничение «за сегодня» и есть «до конца рабочего дня»: с наступлением
    нового дня список сам обнуляется, отдельная чистка по расписанию не нужна.
    """
    from .models import IncomingCallAlert
    if employee is None:
        return IncomingCallAlert.objects.none()
    today = timezone.localdate()
    return (IncomingCallAlert.objects
            .filter(employee=employee, dismissed_at__isnull=True, started_at__date=today)
            .select_related("client")
            .order_by("started_at"))


@login_required
@never_cache
def call_alerts(request):
    """Стек карточек — подгружается при открытии страницы.

    Без этого напоминание жило бы только в разметке и умирало при любом
    обновлении браузера — ровно тогда, когда оно нужнее всего.
    """
    alerts = list(_today_alerts(get_employee(request.user)))
    return render(request, "telephony/partials/_call_alerts.html", {"alerts": alerts})


@login_required
@require_POST
@never_cache
def alert_dismiss(request, alert_id):
    from .models import IncomingCallAlert
    emp = get_employee(request.user)
    (IncomingCallAlert.objects
     .filter(pk=alert_id, employee=emp, dismissed_at__isnull=True)
     .update(dismissed_at=timezone.now()))
    return HttpResponse("")          # карточка заменяется пустотой (hx-swap=outerHTML)


@login_required
@require_POST
@never_cache
def alerts_dismiss_all(request):
    from .models import IncomingCallAlert
    emp = get_employee(request.user)
    ids = list(_today_alerts(emp).values_list("pk", flat=True))
    IncomingCallAlert.objects.filter(pk__in=ids).update(dismissed_at=timezone.now())
    return render(request, "telephony/partials/_call_alerts.html", {"alerts": []})
