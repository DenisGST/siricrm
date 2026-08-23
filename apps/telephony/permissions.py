"""Права доступа к записям телефонных разговоров.

🛑 Записи разговоров — чувствительные данные (в них звучат персональные
сведения клиентов и коммерческие условия), поэтому доступ даётся точечным
флагом ``Employee.can_listen_calls``, а не «по роли заодно». Исключение —
суперюзер и руководство, у которых доступ ко всему и так есть.

Каждая выдача ссылки на файл пишется в ``CallListen`` — журнал прослушиваний.
"""
from functools import wraps

from django.http import HttpResponseForbidden

from apps.core.permissions import is_management


def can_access_calls(user) -> bool:
    """Может открыть раздел «Звонки» и слушать записи."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or is_management(user):
        return True
    emp = getattr(user, "employee", None)
    return bool(emp and emp.can_listen_calls)


def require_calls(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_access_calls(request.user):
            return HttpResponseForbidden("Нет доступа к записям разговоров")
        return view_func(request, *args, **kwargs)

    return _wrapped
