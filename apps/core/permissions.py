"""Центральный модуль разграничения прав SiriCRM.

Архитектура — две оси проверок:
  1. флаг ``user.is_superuser`` — полный доступ ко всему;
  2. поле ``Employee.role`` (см. ``apps.core.models.Employee.ROLE_CHOICES``).

Все проверки в коде должны идти через хелперы из этого модуля,
а не через inline ``user.employee.role == "..."``. Группы ролей менять
здесь, не в десяти view-функциях.

Доменные ограничения (финансы, документы и т.п.) — в своих
``apps.<app>.permissions``; здесь только общая часть.
"""
from functools import wraps

from django.http import HttpResponseForbidden
from rest_framework.permissions import BasePermission, SAFE_METHODS


# ─── Группы ролей ────────────────────────────────────────────
# Поднятый уровень: справочники, sidebar с requires_elevated, ряд CRM-операций.
ELEVATED_ROLES = {"admin", "head_dep"}

# Руководство: + управляющий партнёр. Используется для «видеть чужие канбаны»
# и других просмотровых привилегий.
MANAGEMENT_ROLES = {"admin", "head_dep", "managing_partner"}


# ─── Базовые хелперы ─────────────────────────────────────────
def get_employee(user):
    """Возвращает связанный Employee или None. Не падает на суперюзере без профиля."""
    if not user or not user.is_authenticated:
        return None
    try:
        return user.employee
    except Exception:
        return None


def has_role(user, *roles) -> bool:
    """True, если у пользователя есть Employee с одной из перечисленных ролей."""
    emp = get_employee(user)
    return bool(emp and emp.role in roles)


def is_superuser(user) -> bool:
    return bool(user and user.is_authenticated and user.is_superuser)


def is_admin(user) -> bool:
    """superuser или role=admin."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return has_role(user, "admin")


def is_references_access(user) -> bool:
    """Доступ к справочникам ``/references/``: superuser, admin, head_dep."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return has_role(user, *ELEVATED_ROLES)


def is_management(user) -> bool:
    """superuser, admin, head_dep, managing_partner."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return has_role(user, *MANAGEMENT_ROLES)


# ─── Декораторы для Django view-функций ──────────────────────
def _make_require(predicate, message):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not predicate(request.user):
                return HttpResponseForbidden(message)
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


require_superuser = _make_require(is_superuser, "Только для суперпользователя")
require_admin = _make_require(is_admin, "Нет прав администратора")
require_references_access = _make_require(
    is_references_access, "Нет доступа к справочникам"
)
require_management = _make_require(is_management, "Нет доступа")


# ─── DRF permission-классы ───────────────────────────────────
class _BaseRolePermission(BasePermission):
    """Базовый класс — оборачивает функцию-предикат в DRF-permission."""
    predicate = staticmethod(lambda u: False)
    message = "Недостаточно прав"

    def has_permission(self, request, view):
        return self.predicate(request.user)


class IsAdmin(_BaseRolePermission):
    predicate = staticmethod(is_admin)
    message = "Только для администраторов"


class IsReferencesAccess(_BaseRolePermission):
    predicate = staticmethod(is_references_access)
    message = "Нет доступа к справочникам"


class IsManagement(_BaseRolePermission):
    predicate = staticmethod(is_management)


class ReadOnlyOrIsAdmin(BasePermission):
    """GET/HEAD/OPTIONS — любому авторизованному; write — только admin/superuser."""
    message = "Запись только для администраторов"

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return is_admin(request.user)


class ReadOnlyOrIsManagement(BasePermission):
    """GET — любому авторизованному; write — admin/head_dep/managing_partner/superuser."""
    message = "Запись только для руководства"

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return is_management(request.user)
