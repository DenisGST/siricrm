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

        Видят всё:
          * superuser / admin / managing_partner;
          * Employee.is_owner (root);
          * head_dep (любой руководитель отдела);
          * сотрудник отдела с `Department.sees_all_clients=True`.

        Остальные сотрудники видят клиента, если:
          * они в `Client.employees` (закреплён), ИЛИ
          * они в `Service.employees` (исполнитель услуги клиента), ИЛИ
          * у клиента есть `Service.common_status.department == их отдел`
            (на текущем этапе обслуживания отвечает их отдел; см.
            `ServiceCommonStatus.department`).
        """
        if is_admin(user):
            return self
        emp = get_employee(user)
        if not emp:
            return self.none()
        if getattr(emp, "is_owner", False):
            return self
        if emp.role in ("head_dep", "managing_partner"):
            return self
        if emp.department_id and getattr(emp.department, "sees_all_clients", False):
            return self
        return self.filter(
            models.Q(employees=emp)
            | models.Q(services__employees=emp)
            | models.Q(
                services__common_status__department_id=emp.department_id
            )
        ).distinct()


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
