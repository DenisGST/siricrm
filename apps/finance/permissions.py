"""
Права доступа к финансовому учёту:

* edit (создание/редактирование платежей и начислений) — admin, accountant, superuser
* delete — admin, superuser
"""
from functools import wraps

from django.http import HttpResponseForbidden


EDIT_ROLES = {"admin", "accountant"}
DELETE_ROLES = {"admin"}


def can_edit_finance(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    emp = getattr(user, "employee", None)
    return bool(emp and emp.role in EDIT_ROLES)


def can_delete_finance(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    emp = getattr(user, "employee", None)
    return bool(emp and emp.role in DELETE_ROLES)


def require_edit(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_edit_finance(request.user):
            return HttpResponseForbidden("Нет прав на редактирование финансов")
        return view_func(request, *args, **kwargs)
    return _wrapped


def require_delete(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_delete_finance(request.user):
            return HttpResponseForbidden("Нет прав на удаление финансов")
        return view_func(request, *args, **kwargs)
    return _wrapped
