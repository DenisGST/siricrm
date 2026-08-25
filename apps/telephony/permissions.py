"""Права доступа к записям телефонных разговоров.

Модель простая и объясняется одной фразой: **свои звонки слушает каждый,
чужие — только руководство**.

- раздел «Звонки» открыт любому сотруднику;
- рядовой видит и слушает записи, привязанные к его внутреннему номеру
  (`Call.employee == он`);
- руководство (admin / head_dep / managing_partner) и суперюзер — всё;
- `Employee.can_listen_calls` — точечная выдача доступа к ЧУЖИМ записям тому,
  кто не руководитель (например, контролю качества).

🛑 Записи разговоров содержат персональные данные клиентов, поэтому каждая
выдача ссылки на файл пишется в ``CallListen`` — независимо от того, свой это
звонок или чужой.
"""
from functools import wraps

from django.http import HttpResponseForbidden

from apps.core.permissions import get_employee, is_management


def can_access_calls(user) -> bool:
    """Может открыть раздел «Звонки». Открыт всем сотрудникам."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return get_employee(user) is not None


def can_listen_all_calls(user) -> bool:
    """Может слушать ЧУЖИЕ записи, а не только свои."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or is_management(user):
        return True
    emp = get_employee(user)
    return bool(emp and emp.can_listen_calls)


def can_listen_call(user, call) -> bool:
    """Может ли этот пользователь слушать ЭТУ запись."""
    if call is None:
        return False
    if can_listen_all_calls(user):
        return True
    emp = get_employee(user)
    return bool(emp and call.employee_id == emp.pk)


def require_calls(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_access_calls(request.user):
            return HttpResponseForbidden("Нет доступа к разделу звонков")
        return view_func(request, *args, **kwargs)

    return _wrapped


def can_handle_all_missed(user) -> bool:
    """Видит ВЕСЬ реестр пропущенных, а не только свои направления.

    То же множество, что и у чужих записей разговоров: руководство, суперюзер
    и точечный флаг ``can_listen_calls`` (контроль качества).
    """
    return can_listen_all_calls(user)


def visible_missed(user):
    """Реестр пропущенных, отфильтрованный по правам. → queryset.

    🛑 Фильтр стоит на queryset'е, а не в шаблоне — как и в журнале звонков:
    иначе через ?page= рядовой сотрудник вычитал бы номера и ФИО клиентов
    чужих направлений.

    Рядовой видит обращения тех групп, к которым относится: его отдел, его
    подписка, его внутренний номер. Плюс — свои же взятые в работу, даже если
    состав группы с тех пор поменялся.
    """
    from django.db.models import Q

    from .models import MissedCall

    qs = MissedCall.objects.select_related("group", "client", "assignee__user",
                                           "recording", "call")
    if can_handle_all_missed(user):
        return qs
    emp = get_employee(user)
    if emp is None:
        return qs.none()

    cond = Q(assignee=emp)
    if emp.department_id:
        cond |= Q(group__department_id=emp.department_id, group__notify_department=True)
    cond |= Q(group__subscribers=emp)
    if emp.sip_extension:
        cond |= Q(extension=emp.sip_extension)
    # 🛑 distinct обязателен: подписчики — M2M, join размножает строки.
    return qs.filter(cond).distinct()


def can_listen_voicemail(user, missed) -> bool:
    """Голосовое сообщение слушает тот, кто видит само обращение.

    Отдельного права нет намеренно: сообщение и есть суть обращения — без
    него запись в реестре бесполезна, отработать её нельзя.
    """
    if missed is None:
        return False
    return visible_missed(user).filter(pk=missed.pk).exists()
