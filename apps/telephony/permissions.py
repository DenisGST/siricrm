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
