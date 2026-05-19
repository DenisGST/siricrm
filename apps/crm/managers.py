"""QuerySet-фильтры для CRM на основе тех же правил, что и django-rules.

django-rules умеет проверять object-level права (``user.has_perm``),
но НЕ фильтрует QuerySet. Для списков (``clients_list``,
``services_list``, DRF list-action) нужен явный фильтр —
``Client.objects.visible_to(user)`` и ``Service.objects.visible_to(user)``.

Правила фильтрации **должны совпадать** с предикатами в ``apps.crm.rules``.
Если поменялась бизнес-логика — править нужно в обоих местах.
"""
from django.db import models

from apps.core.permissions import is_admin, is_references_access, get_employee


class ClientQuerySet(models.QuerySet):
    def visible_to(self, user) -> "ClientQuerySet":
        """Клиенты, доступные пользователю.

        * admin / superuser — все клиенты;
        * head_dep — клиенты, у которых хотя бы один сотрудник из его отдела;
        * остальные — клиенты, где они в ``Client.employees``.
        """
        if is_admin(user):
            return self
        emp = get_employee(user)
        if not emp:
            return self.none()
        if emp.role == "head_dep" and emp.department_id:
            return self.filter(
                models.Q(employees=emp)
                | models.Q(employees__department_id=emp.department_id)
            ).distinct()
        return self.filter(employees=emp).distinct()


class ServiceQuerySet(models.QuerySet):
    def visible_to(self, user) -> "ServiceQuerySet":
        """Услуги, доступные пользователю.

        * elevated (admin/head_dep/superuser) — все;
        * остальные — услуги, на которые сотрудник назначен
          (``Service.employees``) ИЛИ чей тип услуги в ``services_allowed``.
        """
        if is_references_access(user):
            return self
        emp = get_employee(user)
        if not emp:
            return self.none()
        return self.filter(
            models.Q(employees=emp)
            | models.Q(name_id__in=emp.services_allowed.values_list("id", flat=True))
        ).distinct()
