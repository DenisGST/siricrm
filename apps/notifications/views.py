"""Web-UI уведомлений: бейдж, панель-список, реакция на кнопки."""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Notification
from . import services

# Вкладки панели → набор статусов
TABS = {
    "new":    [Notification.STATUS_NEW],
    "work":   [Notification.STATUS_ACCEPTED],
    "snoozed": [Notification.STATUS_SNOOZED],
    "closed": [Notification.STATUS_DONE, Notification.STATUS_REJECTED,
               Notification.STATUS_ACKNOWLEDGED],
}

# Пресеты «Напомнить позже» (минуты). tomorrow — завтра 10:00 (считается отдельно).
SNOOZE_PRESETS = {
    "15m": 15, "1h": 60, "3h": 180, "tomorrow": None,
}
SNOOZE_LABELS = [
    ("15m", "Через 15 минут"),
    ("1h", "Через час"),
    ("3h", "Через 3 часа"),
    ("tomorrow", "Завтра, 10:00"),
]
TAB_LABELS = [("new", "Новые"), ("work", "В работе"),
              ("snoozed", "Отложенные"), ("closed", "Закрытые")]


def _employee(request):
    try:
        return request.user.employee
    except Exception:
        return None


def _badge_response(employee):
    from apps.realtime.utils import _notif_badge_html
    return HttpResponse(_notif_badge_html(employee))


@login_required
def badge(request):
    """OOB-бейдж колокола (используется поллером в шапке как фолбэк к WS)."""
    emp = _employee(request)
    if emp is None:
        return HttpResponse("")
    return _badge_response(emp)


def _panel_context(request):
    """Контекст панели: items текущей вкладки + счётчики по всем вкладкам."""
    emp = _employee(request)
    tab = (request.GET.get("tab") or request.POST.get("tab") or "new")
    if tab not in TABS:
        tab = "new"
    items = []
    counts = {"new": 0, "work": 0, "snoozed": 0, "closed": 0}
    if emp is not None:
        base = Notification.objects.filter(recipient=emp).select_related(
            "client", "source", "source__event_type", "source__action_type",
        )
        counts["new"] = base.filter(status=Notification.STATUS_NEW).count()
        counts["work"] = base.filter(status=Notification.STATUS_ACCEPTED).count()
        counts["snoozed"] = base.filter(status=Notification.STATUS_SNOOZED).count()
        counts["closed"] = base.filter(status__in=TABS["closed"]).count()
        items = list(base.filter(status__in=TABS[tab])[:100])
    tabs = [{"key": k, "label": lbl, "count": counts[k]} for k, lbl in TAB_LABELS]
    return {"items": items, "tab": tab, "tabs": tabs, "snooze_labels": SNOOZE_LABELS}


@login_required
def panel(request):
    """Полная панель-модалка (шелл + шапка + тело). Открытие по клику на колокол."""
    return render(request, "notifications/panel.html", _panel_context(request))


@login_required
def panel_list(request):
    """Только тело панели (вкладки+список) — HTMX-обновление без мигания шелла."""
    return render(request, "notifications/partials/panel_inner.html", _panel_context(request))


@login_required
@require_POST
def respond(request, pk, action):
    """Реакция на уведомление: accept|done|reject|snooze.

    Возвращает обновлённое тело панели (текущая вкладка) + OOB-бейдж.
    """
    emp = _employee(request)
    if emp is None:
        return HttpResponseBadRequest("no employee")
    if action not in services.RESPONSE_MAP:
        return HttpResponseBadRequest("bad action")

    n = Notification.objects.filter(pk=pk, recipient=emp).first()
    if n is None:
        return HttpResponseBadRequest("not found")

    snooze_until = None
    if action == "snooze":
        snooze_until = _parse_snooze(
            request.POST.get("preset", "tomorrow"),
            request.POST.get("snooze_at"),
        )

    services.respond(n, action, employee=emp, via="web",
                     snooze_until=snooze_until, comment=request.POST.get("comment", ""))

    # Перерисовываем только тело панели (текущая вкладка) + OOB-бейдж.
    from apps.realtime.utils import _notif_badge_html
    resp = render(request, "notifications/partials/panel_inner.html", _panel_context(request))
    resp.write(_notif_badge_html(emp))
    return resp


def _parse_snooze(preset: str, snooze_at: str = None):
    """Вернуть datetime, до которого отложить.

    snooze_at — явные дата/время из <input type="datetime-local"> (приоритет).
    preset — предустановка (15m/1h/3h/tomorrow или «<N><m|h|d>»). default —
    завтра 10:00 МСК.
    """
    from django.utils.dateparse import parse_datetime

    now = timezone.localtime()

    # Явная дата/время — приоритет.
    if snooze_at:
        dt = parse_datetime(snooze_at)
        if dt is not None:
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return dt

    if preset in SNOOZE_PRESETS and SNOOZE_PRESETS[preset]:
        return now + timedelta(minutes=SNOOZE_PRESETS[preset])
    # custom: «<N><m|h|d>»
    if preset and preset[-1] in "mhd" and preset[:-1].isdigit():
        val = int(preset[:-1])
        unit = {"m": "minutes", "h": "hours", "d": "days"}[preset[-1]]
        return now + timedelta(**{unit: val})
    # tomorrow 10:00
    tomorrow = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    return tomorrow
