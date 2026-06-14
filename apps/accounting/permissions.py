"""Права доступа к разделу «Бухгалтерский учёт» (рабочее место бухгалтера).

Раздел видят: суперюзер, роль admin/accountant, а также руководство
(head_dep / managing_partner — через is_references_access). Менеджеры/агенты —
нет. Гейт пункта меню — в apps.core.context_processors (по url /accounting/),
гейт вьюх — декоратор require_accounting.
"""
from functools import wraps

from django.http import HttpResponseForbidden

from apps.core.permissions import is_references_access

# Роли, которым положено рабочее место бухгалтера (помимо руководства/суперюзера).
ACCOUNTING_ROLES = {"accountant", "admin"}


def can_access_accounting(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    emp = getattr(user, "employee", None)
    if emp and emp.role in ACCOUNTING_ROLES:
        return True
    return is_references_access(user)


def require_accounting(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_access_accounting(request.user):
            return HttpResponseForbidden("Нет доступа к разделу бухгалтерии")
        return view_func(request, *args, **kwargs)

    return _wrapped
