"""daily_stats handler: статистика прод-сервера за текущие сутки.

Запускается в devops-runner (есть Django ORM + доступ к БД). Возвращает
готовый текстовый отчёт (output) + структуру (result). Используется
Telegram-кнопкой «Статистика» в мониторинг-боте (dev дёргает прод-агент).

Метрики за сегодня (по локальному времени сервера):
- сообщения исходящие/входящие, разбивка по каналам;
- сформировано документов (АФД GeneratedDocument);
- рабочее время сотрудников по парам login/logout из EmployeeLog;
- сколько онлайн прямо сейчас.
"""
from datetime import timedelta

from django.utils import timezone

from apps.devops.tasks import register_handler


def _fmt_hm(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h and m:
        return f"{h}ч {m}м"
    if h:
        return f"{h}ч"
    return f"{m}м"


def _messages_block(day_start):
    from apps.crm.models import Message
    from django.db.models import Count

    qs = Message.objects.filter(created_at__gte=day_start)
    out_total = qs.filter(direction="outgoing").count()
    in_total = qs.filter(direction="incoming").count()

    by_chan = {}
    for row in (qs.values("direction", "channel")
                  .annotate(n=Count("id")).order_by()):
        ch = (row["channel"] or "—")
        by_chan.setdefault(ch, {"in": 0, "out": 0})
        by_chan[ch]["out" if row["direction"] == "outgoing" else "in"] = row["n"]
    return out_total, in_total, by_chan


def _clients_block(day_start):
    """Сколько новых клиентов (status=lead+active) за сегодня + всего leads."""
    from apps.crm.models import Client
    return Client.objects.filter(created_at__gte=day_start).count()


def _consultations_block(day_start, now):
    """Консультации за сегодня — проведённые/назначенные/перенесённые/отменённые."""
    from django.db.models import Count
    from apps.consultations.models import Consultation
    by_status = dict(
        Consultation.objects
        .filter(datetime_start__gte=day_start, datetime_start__lt=now.replace(hour=23, minute=59))
        .values("status").annotate(n=Count("id")).values_list("status", "n")
    )
    return {
        "done": by_status.get("done", 0),
        "booked": by_status.get("booked", 0),
        "transferred": by_status.get("transferred", 0),
        "cancelled": by_status.get("cancelled", 0),
    }


def _payments_block(day_start):
    """Суммы платежей за сегодня по направлениям.

    Payment.payment_date — DateField (без времени). Берём по дате
    day_start (= сегодня по локальному TZ сервера).
    """
    from django.db.models import Sum
    from apps.finance.models import Payment

    today = day_start.date()
    qs = Payment.objects.filter(payment_date=today)
    in_sum = qs.filter(direction="in").aggregate(s=Sum("amount_in"))["s"] or 0
    out_sum = qs.filter(direction="out").aggregate(s=Sum("amount_out"))["s"] or 0
    in_cnt = qs.filter(direction="in").count()
    out_cnt = qs.filter(direction="out").count()
    return {
        "incoming_sum": float(in_sum),
        "incoming_count": in_cnt,
        "outgoing_sum": float(out_sum),
        "outgoing_count": out_cnt,
    }


def _worktime_block(day_start, now):
    """Рабочее время за сегодня по EmployeeLog (login/logout).

    Логика на сотрудника: идём по событиям дня по времени; на 'login'
    открываем интервал, на 'logout' закрываем (начало = предыдущий login,
    либо начало суток, если login был вчера). Открытый в конце интервал
    (ещё онлайн) тянем до now. Если сегодня событий нет, но сотрудник
    is_online — считаем онлайн с начала суток.
    """
    from django.db.models import Max
    from apps.core.models import Employee, EmployeeLog

    logs = (EmployeeLog.objects
            .filter(action__in=["login", "logout"], timestamp__gte=day_start)
            .order_by("employee_id", "timestamp")
            .values_list("employee_id", "action", "timestamp"))

    per_emp = {}
    for emp_id, action, ts in logs:
        per_emp.setdefault(emp_id, []).append((action, ts))

    # Только реальные сотрудники (служебный бот-пользователь — user.is_active=False).
    active_ids = set(Employee.objects.filter(user__is_active=True).values_list("id", flat=True))
    per_emp = {eid: ev for eid, ev in per_emp.items() if eid in active_ids}
    online_ids = set(Employee.objects.filter(is_online=True, user__is_active=True)
                     .values_list("id", flat=True))
    # Последняя активность за сегодня (любое событие лога) — чтобы закрыть
    # «login без logout» у тех, кого heartbeat уже увёл в офлайн без записи
    # logout (sync_employee_status флипает is_online, не пишет logout).
    last_act = dict(
        EmployeeLog.objects.filter(timestamp__gte=day_start, employee_id__in=active_ids)
        .values("employee_id").annotate(m=Max("timestamp")).values_list("employee_id", "m")
    )

    seconds_by_emp = {}
    for emp_id, events in per_emp.items():
        total = 0.0
        open_start = None
        first = True
        for action, ts in events:
            if action == "login":
                if open_start is None:        # повторный login (мультивкладка) — игнор
                    open_start = ts
            else:  # logout
                if open_start is not None:    # закрываем открытую сессию
                    total += (ts - open_start).total_seconds()
                    open_start = None
                elif first:                   # вошёл до полуночи → с начала суток
                    total += (ts - day_start).total_seconds()
                # повторный logout без открытой сессии — игнор
            first = False
        if open_start is not None:  # сессия не закрыта logout'ом
            if emp_id in online_ids:
                end = now  # реально ещё онлайн
            else:
                end = last_act.get(emp_id, open_start)  # офлайн → до последней активности
            total += max(0.0, (end - open_start).total_seconds())
        seconds_by_emp[emp_id] = total

    # Онлайн без событий сегодня (зашёл вчера, не выходил) — с начала суток.
    for emp_id in online_ids:
        if emp_id not in seconds_by_emp:
            seconds_by_emp[emp_id] = (now - day_start).total_seconds()

    # Имена
    names = dict(
        (e.id, f"{e.user.last_name} {e.user.first_name}".strip() or e.user.get_username())
        for e in Employee.objects.filter(id__in=seconds_by_emp.keys()).select_related("user")
    )
    rows = sorted(
        ((names.get(eid, str(eid)), sec, eid in online_ids)
         for eid, sec in seconds_by_emp.items()),
        key=lambda r: r[1], reverse=True,
    )
    total_seconds = sum(seconds_by_emp.values())
    return rows, total_seconds, len(online_ids)


@register_handler("daily_stats")
def run_daily_stats(params: dict) -> dict:
    now = timezone.localtime(timezone.now())
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    out_total, in_total, by_chan = _messages_block(day_start)

    from apps.afd.models import GeneratedDocument
    docs = GeneratedDocument.objects.filter(created_at__gte=day_start).count()

    new_clients = _clients_block(day_start)
    cons = _consultations_block(day_start, now)
    pays = _payments_block(day_start)

    rows, total_work, online_now = _worktime_block(day_start, now)

    # Текстовый отчёт
    lines = [
        f"📊 Статистика за сутки (с 00:00, {now.strftime('%d.%m.%Y %H:%M')})",
        "",
        f"🆕 Новых клиентов: {new_clients}",
        "",
        (
            f"📅 Консультации: ✅ {cons['done']} проведено · "
            f"📌 {cons['booked']} назначено · "
            f"↻ {cons['transferred']} перенесено · "
            f"❌ {cons['cancelled']} отменено"
        ),
        "",
        f"✉️ Сообщения: отправлено {out_total}, получено {in_total}",
    ]
    for ch in sorted(by_chan):
        c = by_chan[ch]
        lines.append(f"    {ch}: ↗{c['out']} ↘{c['in']}")
    lines += [
        "",
        (
            f"💰 Платежи: входящие {pays['incoming_sum']:,.0f} ₽ ({pays['incoming_count']} шт) · "
            f"исходящие {pays['outgoing_sum']:,.0f} ₽ ({pays['outgoing_count']} шт)"
        ),
        "",
        f"📄 Документов сформировано (АФД): {docs}",
        "",
        f"👥 Рабочее время (онлайн сейчас: {online_now}):",
    ]
    if rows:
        for name, sec, is_on in rows[:25]:
            mark = " 🟢" if is_on else ""
            lines.append(f"    {name}: {_fmt_hm(sec)}{mark}")
        lines.append(f"    — Итого: {_fmt_hm(total_work)}")
    else:
        lines.append("    нет данных за сегодня")

    return {
        "output": "\n".join(lines),
        "result": {
            "new_clients": new_clients,
            "consultations": cons,
            "payments": pays,
            "messages": {"out": out_total, "in": in_total, "by_channel": by_chan},
            "documents": docs,
            "online_now": online_now,
            "worktime": [
                {"name": n, "seconds": int(s), "online": o} for n, s, o in rows
            ],
            "worktime_total_seconds": int(total_work),
        },
    }
