"""Object-level правила доступа для CRM (клиенты / услуги).

Подключается django-rules через ``AutodiscoverRulesConfig``.
Регистрирует пермишены ``crm.view_client``, ``crm.edit_client``,
``crm.delete_client``, ``crm.view_service``, ``crm.edit_service``,
``crm.delete_service``.

Правила видимости (бизнес-логика) — согласованы с
``Client.objects.visible_to`` / ``Service.objects.visible_to`` в
``apps/crm/managers.py``. Менять надо синхронно.

* **Клиент** — видят всех: admin/superuser, head_dep, managing_partner,
  Employee.is_owner, сотрудник отдела с Department.sees_all_clients=True.
  Остальные — клиента, где они в ``Client.employees`` ИЛИ в
  ``Service.employees`` его услуг ИЛИ у его услуги ``common_status.department``
  совпадает с их отделом.
* **Услуга** — admin/head_dep/superuser — все; остальные — назначенные
  (``Service.employees``) или тип услуги в ``services_allowed``.

Querysets фильтруются не здесь, а через ``visible_to(user)`` менеджеры.
django-rules сам QuerySet не фильтрует.
"""
import rules

from apps.core.permissions import (
    is_admin,
    is_management,
    is_references_access,
    get_employee,
)


# ─── Базовые предикаты-обёртки ─────────────────────────────
is_admin_p = rules.predicate(is_admin, name="is_admin")
is_management_p = rules.predicate(is_management, name="is_management")
is_references_access_p = rules.predicate(
    is_references_access, name="is_references_access"
)


@rules.predicate
def is_owner(user, _obj=None):
    """Employee.is_owner — root-флаг основателя."""
    emp = get_employee(user)
    return bool(emp and getattr(emp, "is_owner", False))


@rules.predicate
def dept_sees_all_clients(user, _obj=None):
    """Сотрудник отдела с ``Department.sees_all_clients=True`` (обычно «Отдел продаж»)."""
    emp = get_employee(user)
    return bool(
        emp
        and emp.department_id
        and getattr(emp.department, "sees_all_clients", False)
    )


# ─── Object-level предикаты: клиенты ───────────────────────
@rules.predicate
def is_responsible_for_client(user, client):
    """Пользователь — один из ответственных сотрудников клиента."""
    emp = get_employee(user)
    if not emp or not client:
        return False
    return client.employees.filter(pk=emp.pk).exists()


@rules.predicate
def is_responsible_for_client_service(user, client):
    """Назначен исполнителем хотя бы на одну услугу клиента (``Service.employees``)."""
    emp = get_employee(user)
    if not emp or not client:
        return False
    return client.services.filter(employees=emp).exists()


@rules.predicate
def is_in_client_service_step_dept(user, client):
    """У клиента есть услуга на этапе, закреплённом за отделом сотрудника
    (``Service.common_status.department == emp.department``)."""
    emp = get_employee(user)
    if not emp or not client or not emp.department_id:
        return False
    return client.services.filter(
        common_status__department_id=emp.department_id
    ).exists()


# ─── Object-level предикаты: услуги ────────────────────────
@rules.predicate
def is_responsible_for_service(user, service):
    """Сотрудник назначен на услугу (Service.employees)."""
    emp = get_employee(user)
    if not emp or not service:
        return False
    return service.employees.filter(pk=emp.pk).exists()


@rules.predicate
def is_allowed_service_type(user, service):
    """У сотрудника тип услуги доступен (Employee.services_allowed)."""
    emp = get_employee(user)
    if not emp or not service:
        return False
    return emp.services_allowed.filter(pk=service.name_id).exists()


# ─── Композиции и регистрация пермишенов ───────────────────
# Клиент — согласован с Client.objects.visible_to.
view_client = (
    is_management_p                       # admin / head_dep / managing_partner / superuser
    | is_owner                            # Employee.is_owner
    | dept_sees_all_clients               # Department.sees_all_clients=True
    | is_responsible_for_client           # Client.employees
    | is_responsible_for_client_service   # Service.employees услуг клиента
    | is_in_client_service_step_dept      # Service.common_status.department == моему отделу
)
edit_client = view_client          # кто видит, тот и редактирует
delete_client = is_admin_p          # удаление только admin/superuser

rules.add_perm("crm.view_client", view_client)
rules.add_perm("crm.edit_client", edit_client)
rules.add_perm("crm.delete_client", delete_client)

# Услуга
view_service = (
    is_admin_p
    | is_references_access_p
    | is_responsible_for_service
    | is_allowed_service_type
)
edit_service = view_service
delete_service = is_references_access_p   # admin/head_dep/superuser

rules.add_perm("crm.view_service", view_service)
rules.add_perm("crm.edit_service", edit_service)
rules.add_perm("crm.delete_service", delete_service)
