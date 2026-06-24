"""Права доступа к разделу «Процедуры банкротства» (рабочее место юриста/АУ).

Раздел видят: суперюзер, юр-роли (юрист, помощник юриста, АУ, помощник АУ,
admin), а также руководство (head_dep / managing_partner — через is_management).
Менеджеры/операторы/агенты — нет. Объектная видимость услуги дополнительно
режется `Service.objects.visible_to(user)` в самих вьюхах.
"""
from functools import wraps

from django.http import HttpResponseForbidden

from apps.core.permissions import is_management

# Роли, которым положено рабочее место юриста (помимо руководства/суперюзера).
# Литерал `assitent_legal` — опечатка в Employee.ROLE_CHOICES, используем как есть.
PROCEDURE_ROLES = {"lawyer", "assitent_legal", "arbitration", "arbitr_assistant", "admin"}


def can_access_procedures(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    emp = getattr(user, "employee", None)
    if emp and emp.role in PROCEDURE_ROLES:
        return True
    return is_management(user)


def require_procedures(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_access_procedures(request.user):
            return HttpResponseForbidden("Нет доступа к разделу процедур")
        return view_func(request, *args, **kwargs)

    return _wrapped
