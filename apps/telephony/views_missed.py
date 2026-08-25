"""Реестр «Пропущенные» — вкладка раздела «Звонки».

Что здесь важно понимать: реестр показывает не историю, а ДОЛГ перед
звонившими. Поэтому по умолчанию открыт фильтр «требуют ответа», а строка
уходит из него сама, как только с номером состоялся разговор
(``missed.close_open_for_phone`` из разбора CDR).

🛑 Видимость режется на queryset'е (``permissions.visible_missed``), а не в
шаблоне: иначе через ?page= рядовой сотрудник вычитал бы номера и ФИО клиентов
чужих направлений.
"""
import datetime
import logging
import re

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.core.permissions import get_employee

from .models import CallGroup, MissedCall
from .permissions import (can_handle_all_missed, can_listen_voicemail,
                          require_calls, visible_missed)

logger = logging.getLogger(__name__)

PER_PAGE_CHOICES = (25, 50, 100)
DEFAULT_PER_PAGE = 50
VOICEMAIL_URL_TTL = 600

SORT_FIELDS = {
    "occurred": ("occurred_at",),
    "phone": ("phone", "occurred_at"),
    "client": ("client__last_name", "client__first_name", "occurred_at"),
    "group": ("group__name", "occurred_at"),
    "kind": ("kind", "occurred_at"),
    "status": ("status", "occurred_at"),
}
DEFAULT_SORT = "occurred"

# Пресет фильтра «состояние». Открытым по умолчанию показываем только то,
# что ещё не отработано: реестр — рабочая очередь, а не архив.
STATE_OPEN = "open"
STATE_ALL = "all"
STATE_CHOICES = [
    (STATE_OPEN, "Требуют ответа"),
    (MissedCall.STATUS_NEW, "Новые"),
    (MissedCall.STATUS_IN_WORK, "В работе"),
    (MissedCall.STATUS_DONE, "Отработанные"),
    (MissedCall.STATUS_AUTO_DONE, "Связались автоматически"),
    (MissedCall.STATUS_IGNORED, "Не требуют ответа"),
    (STATE_ALL, "Все"),
]


def _parse_date(raw):
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _filtered(request):
    qs = visible_missed(request.user)

    state = (request.GET.get("state") or STATE_OPEN).strip()
    if state == STATE_OPEN:
        qs = qs.filter(status__in=MissedCall.OPEN_STATUSES)
    elif state in dict(MissedCall.STATUS_CHOICES):
        qs = qs.filter(status=state)

    kind = (request.GET.get("kind") or "").strip()
    if kind in dict(MissedCall.KIND_CHOICES):
        qs = qs.filter(kind=kind)

    group_id = (request.GET.get("group") or "").strip()
    if group_id.isdigit():
        qs = qs.filter(group_id=int(group_id))

    q = (request.GET.get("q") or "").strip()
    if q:
        cond = Q(raw_phone__icontains=q)
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 3:
            variants = {digits}
            if digits[0] in "78":
                variants.add(digits[1:])
            for v in variants:
                if len(v) >= 3:
                    cond |= Q(phone__contains=v) | Q(raw_phone__contains=v)
        for word in q.split():
            cond |= (Q(client__first_name__icontains=word)
                     | Q(client__last_name__icontains=word))
        qs = qs.filter(cond)

    date_from = _parse_date(request.GET.get("date_from"))
    if date_from:
        qs = qs.filter(occurred_at__gte=timezone.make_aware(
            datetime.datetime.combine(date_from, datetime.time.min)))
    date_to = _parse_date(request.GET.get("date_to"))
    if date_to:
        qs = qs.filter(occurred_at__lte=timezone.make_aware(
            datetime.datetime.combine(date_to, datetime.time.max)))
    return qs


def _sorted(qs, sort, direction):
    fields = SORT_FIELDS.get(sort) or SORT_FIELDS[DEFAULT_SORT]
    prefix = "" if direction == "asc" else "-"
    order = [f"{prefix}{f}" for f in fields]
    order.append(f"{prefix}id")     # тай-брейк, иначе строки прыгают по страницам
    return qs.order_by(*order)


def missed_context(request):
    qs = _filtered(request)

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

    # Сводка — по всей выборке в SQL, а не по странице.
    stats = qs.aggregate(
        new=Count("id", filter=Q(status=MissedCall.STATUS_NEW)),
        in_work=Count("id", filter=Q(status=MissedCall.STATUS_IN_WORK)),
        voicemail=Count("id", filter=Q(kind=MissedCall.KIND_VOICEMAIL)),
        known=Count("id", filter=Q(client__isnull=False)),
    )

    rows = list(page_obj.object_list)

    f = {k: request.GET.get(k, "") for k in
         ("q", "state", "kind", "group", "date_from", "date_to")}
    f["state"] = f["state"] or STATE_OPEN

    return {
        "page_obj": page_obj,
        "paginator": paginator,
        "missed_rows": rows,
        "page_range": list(paginator.get_elided_page_range(
            page_obj.number, on_each_side=1, on_ends=1)),
        "ellipsis": Paginator.ELLIPSIS,
        "total": paginator.count,
        "row_offset": (page_obj.number - 1) * per_page,
        "stat_new": stats["new"] or 0,
        "stat_in_work": stats["in_work"] or 0,
        "stat_voicemail": stats["voicemail"] or 0,
        "stat_known": stats["known"] or 0,
        "sort": sort,
        "direction_sort": direction,
        "per_page": per_page,
        "per_page_choices": PER_PAGE_CHOICES,
        "kinds": MissedCall.KIND_CHOICES,
        "states": STATE_CHOICES,
        "groups": CallGroup.objects.filter(is_active=True),
        "me": get_employee(request.user),
        "can_handle_all": can_handle_all_missed(request.user),
        "f": f,
        "oob": request.headers.get("HX-Request") == "true",
    }


@login_required
@require_calls
@never_cache
def missed_list(request):
    """Таблица реестра — HTMX-партиал (фильтры, сортировка, пагинация)."""
    return render(request, "telephony/partials/_missed_list.html",
                  missed_context(request))


def _get_row(request, missed_id):
    """Запись реестра с проверкой права на неё.

    🛑 Право проверяем на КОНКРЕТНУЮ запись, а не только на раздел: кнопки
    зовутся по id, минуя таблицу.
    """
    row = get_object_or_404(MissedCall, pk=missed_id)
    if not visible_missed(request.user).filter(pk=row.pk).exists():
        return None
    return row


def _render_row(request, row, saved=False):
    return render(request, "telephony/partials/_missed_row.html", {
        "row": row, "me": get_employee(request.user),
        "can_handle_all": can_handle_all_missed(request.user),
        "saved": saved,
    })


@login_required
@require_calls
@require_POST
@never_cache
def missed_take(request, missed_id):
    """«Беру» — обращение закрепляется за сотрудником.

    Смысл кнопки не в статусе, а в том, чтобы коллеги не перезванивали
    одному человеку вчетвером: уведомление уходит всей группе сразу.
    """
    row = _get_row(request, missed_id)
    if row is None:
        return HttpResponse(status=403)
    emp = get_employee(request.user)
    if row.status in MissedCall.OPEN_STATUSES:
        row.status = MissedCall.STATUS_IN_WORK
        row.assignee = emp
        row.save(update_fields=["status", "assignee", "updated_at"])
    return _render_row(request, row)


@login_required
@require_calls
@require_POST
@never_cache
def missed_close(request, missed_id):
    """Закрыть обращение: «отработан» или «не требует ответа».

    Второе — для роботов, ошибочных наборов и спам-обзвонов: их в потоке
    заметная доля, и без такой кнопки очередь не разгребается.
    """
    row = _get_row(request, missed_id)
    if row is None:
        return HttpResponse(status=403)
    emp = get_employee(request.user)
    status = (request.POST.get("status") or MissedCall.STATUS_DONE).strip()
    if status not in (MissedCall.STATUS_DONE, MissedCall.STATUS_IGNORED):
        status = MissedCall.STATUS_DONE
    row.status = status
    row.handled_at = timezone.now()
    row.handled_by = emp
    if row.assignee_id is None:
        row.assignee = emp
    row.save(update_fields=["status", "handled_at", "handled_by", "assignee", "updated_at"])
    _log_client_action(row, emp, "Пропущенный звонок отработан"
                       if status == MissedCall.STATUS_DONE else "")
    return _render_row(request, row)


@login_required
@require_calls
@require_POST
@never_cache
def missed_reopen(request, missed_id):
    """Вернуть в работу — закрыли по ошибке (в т.ч. автозакрытием)."""
    row = _get_row(request, missed_id)
    if row is None:
        return HttpResponse(status=403)
    row.status = MissedCall.STATUS_NEW
    row.handled_at = None
    row.handled_by = None
    row.closed_by_call = None
    row.save(update_fields=["status", "handled_at", "handled_by",
                            "closed_by_call", "updated_at"])
    return _render_row(request, row)


@login_required
@require_calls
@require_POST
@never_cache
def missed_comment(request, missed_id):
    """Заметка к обращению.

    🛑 Заметки НАКАПЛИВАЮТСЯ построчно (HH:MM — текст), как на карточке
    звонка: по обращению перезванивают не с первого раза, и затирающая
    новая запись стирала бы историю попыток.
    """
    row = _get_row(request, missed_id)
    if row is None:
        return HttpResponse(status=403)
    text = (request.POST.get("comment") or "").strip()[:2000]
    if not text:
        return _render_row(request, row)
    emp = get_employee(request.user)
    stamp = timezone.localtime().strftime("%H:%M")
    row.comment = (f"{row.comment}\n" if row.comment else "") + f"{stamp} — {text}"
    row.save(update_fields=["comment", "updated_at"])
    _log_client_action(row, emp, text)
    return _render_row(request, row, saved=True)


def _log_client_action(row, employee, text: str):
    """Дублируем заметку в событийку клиента — если он опознан.

    Иначе она осталась бы видна только в реестре, а история общения с
    клиентом живёт в его карточке.
    """
    if not row.client_id or not text:
        return
    try:
        from apps.crm.client_log import record_action
        record_action(row.client, "call_client", employee=employee,
                      comment=f"Пропущенный {row.phone or row.raw_phone}: {text}")
    except Exception:  # noqa: BLE001 — событийка не должна ронять реестр
        logger.debug("не удалось записать заметку в событийку", exc_info=True)


@login_required
@require_calls
@never_cache
def missed_voicemail(request, missed_id):
    """Временная ссылка на голосовое сообщение + запись факта прослушивания.

    🛑 Единственная точка выдачи ссылки. В сообщении звучит клиент —
    прослушивание протоколируется так же, как у записей разговоров.
    """
    from .models import CallListen

    row = get_object_or_404(
        MissedCall.objects.select_related("recording"), pk=missed_id)
    if not can_listen_voicemail(request.user, row):
        return JsonResponse({"error": "forbidden"}, status=403)
    if not row.recording_id:
        return JsonResponse({"error": "no_recording"}, status=404)

    from apps.files.s3_utils import get_presigned_url
    try:
        url = get_presigned_url(row.recording.bucket, row.recording.key,
                                expiration=VOICEMAIL_URL_TTL,
                                inline=True, content_type="audio/mpeg")
    except Exception:  # noqa: BLE001
        return JsonResponse({"error": "storage_unavailable"}, status=502)
    if not url:
        return JsonResponse({"error": "storage_unavailable"}, status=502)

    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")
    CallListen.objects.create(
        call=row.call, missed_call=row, employee=get_employee(request.user),
        ip=ip or None,
    )
    return JsonResponse({"url": url, "expires_in": VOICEMAIL_URL_TTL})
