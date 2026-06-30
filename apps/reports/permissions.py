"""Права доступа к разделу «Отчёты».

Раздел видят: суперюзер, руководство (admin / head_dep / managing_partner —
через is_management) и все, у кого есть доступ к бухгалтерии
(can_access_accounting: бухгалтеры + руководство). Гейт пункта меню — в
apps.core.context_processors (по url /reports/), гейт вьюх — декоратор
require_reports.
"""
from functools import wraps

from django.http import HttpResponseForbidden

from apps.core.permissions import is_management
from apps.accounting.permissions import can_access_accounting


def can_access_reports(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return is_management(user) or can_access_accounting(user)


def require_reports(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_access_reports(request.user):
            return HttpResponseForbidden("Нет доступа к разделу отчётов")
        return view_func(request, *args, **kwargs)

    return _wrapped
