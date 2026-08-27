"""Права доступа к рабочему месту оператора колл-центра.

Доску видят: суперюзер, руководство (admin / head_dep / managing_partner) и
сотрудники с флагом ``Employee.can_access_callcenter``.

🛑 Гейт по роли ``operator`` был бы дырой: в модели Employee это ЗНАЧЕНИЕ ПО
УМОЛЧАНИЮ (``role = models.CharField(..., default="operator")``), то есть
роль всех, кому её ни разу не меняли. Поэтому доступ — точечным флагом.

Настройку колонок (Панель управления → «Колл-центр») правит только админ —
как и остальные вкладки панели, через ``is_admin``.
"""
from functools import wraps

from django.http import HttpResponseForbidden

from apps.core.permissions import get_employee, is_management


def is_callcenter_operator(user) -> bool:
    """Оператор колл-центра в узком смысле — по явному флагу.

    🛑 Отличается от ``can_access_callcenter``: та пускает на доску ещё и
    руководство (для обзора чужих канбанов). Узкая проверка нужна там, где
    речь о РАБОТЕ оператора, а не о просмотре, — прежде всего для модалки
    результата звонка, всплывающей после каждого разговора.
    """
    if not user or not user.is_authenticated:
        return False
    emp = get_employee(user)
    return bool(emp and emp.can_access_callcenter)


def can_access_callcenter(user) -> bool:
    """Может открыть доску колл-центра."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if is_management(user):
        return True
    emp = get_employee(user)
    return bool(emp and emp.can_access_callcenter)


def require_callcenter(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_access_callcenter(request.user):
            return HttpResponseForbidden("Нет доступа к рабочему месту колл-центра")
        return view_func(request, *args, **kwargs)

    return _wrapped
