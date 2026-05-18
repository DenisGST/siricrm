"""
Права доступа к финансовому учёту:

* edit (создание/редактирование платежей и начислений) — admin, accountant, superuser
* delete — admin, superuser
"""
from functools import wraps

from django.http import HttpResponseForbidden


EDIT_ROLES = {"admin", "accountant"}
DELETE_ROLES = {"admin"}

# Удаление отдельного начисления: более широкий список ролей.
# Для роли "agent" дополнительно требуется быть исполнителем услуги.
DELETE_CHARGE_ROLES = {"admin", "head_dep", "consultant"}
DELETE_CHARGE_ROLES_IF_ASSIGNED = {"agent"}


def can_delete_charge(user, service) -> bool:
    """Удалять/редактировать (для удаления) одно начисление."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    emp = getattr(user, "employee", None)
    if not emp:
        return False
    if emp.role in DELETE_CHARGE_ROLES:
        return True
    if emp.role in DELETE_CHARGE_ROLES_IF_ASSIGNED:
        return service.employees.filter(pk=emp.pk).exists()
    return False


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
