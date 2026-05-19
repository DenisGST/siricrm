"""Object-level правила доступа для CRM (клиенты / услуги).

Подключается django-rules через ``AutodiscoverRulesConfig``.
Регистрирует пермишены ``crm.view_client``, ``crm.edit_client``,
``crm.delete_client``, ``crm.view_service``, ``crm.edit_service``,
``crm.delete_service``.

Правила видимости (бизнес-логика):

* **Клиент** — обычный сотрудник видит только тех, где он есть в
  ``Client.employees``; head_dep видит клиентов своего отдела
  (любой Employee из ``Client.employees`` принадлежит его Department);
  admin/superuser видят всех. См. также ``Client.objects.visible_to``.
* **Услуга** — обычный сотрудник видит услуги, на которые он назначен
  (``Service.employees``) либо чей тип услуги входит в его
  ``services_allowed``; admin/head_dep/superuser видят всё.

Querysets фильтруются не здесь, а через ``visible_to(user)`` менеджеры
(``apps/crm/managers.py``). django-rules сам QuerySet не фильтрует.
"""
import rules

from apps.core.permissions import (
    is_admin,
    is_references_access,
    get_employee,
)


# ─── Базовые предикаты-обёртки ─────────────────────────────
# Превращаем функции из apps.core.permissions в rules-предикаты, чтобы
# их можно было комбинировать через |.

is_admin_p = rules.predicate(is_admin, name="is_admin")
is_references_access_p = rules.predicate(
    is_references_access, name="is_references_access"
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
def is_in_client_department(user, client):
    """Клиент закреплён за сотрудником из того же отдела (для head_dep).

    Считаем «отдел клиента» = отделы любых сотрудников из ``client.employees``.
    """
    emp = get_employee(user)
    if not emp or not client or not emp.department_id:
        return False
    if emp.role != "head_dep":
        return False
    return client.employees.filter(department_id=emp.department_id).exists()


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
    """У сотрудника тип услуги доступен (Employee.services_allowed).

    Сохраняет существующую логику: обычный сотрудник может видеть/менять
    услугу нужного ему типа, даже если не назначен персонально.
    """
    emp = get_employee(user)
    if not emp or not service:
        return False
    return emp.services_allowed.filter(pk=service.name_id).exists()


# ─── Композиции и регистрация пермишенов ───────────────────
# Клиент: admin/su — все; head_dep — свой отдел; остальные — только свои.
# is_references_access_p НЕ включаем (head_dep не должен видеть чужой отдел).
view_client = (
    is_admin_p
    | is_responsible_for_client
    | is_in_client_department
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
