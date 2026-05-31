"""Передача услуги в работу отдела/сотрудника.

Услуга «переезжает»: общий статус (Service.common_status) ставится во
ВХОДНОЙ (первый по order) статус целевого отдела для этой услуги, прежние
исполнители снимаются, новые получатели получают услугу в свой инбокс
«Не принято» (ServiceEmployeeStatus.is_inbox). Передача сотруднику → ему
одному; в отдел → всем активным сотрудникам отдела.

Логируется в СОБЫТИЙКЕ: ActionType `service_transfer` + событие
`dept_assigned` (в отдел) / `employee_assigned` (сотруднику).
"""
import logging

from django.db import transaction

from apps.crm import client_log
from apps.crm.kanban_inbox import ensure_inbox_status

logger = logging.getLogger(__name__)


def valid_target_department_ids(service):
    """ID отделов, которые ведут услугу этого клиента (есть общие статусы
    для service.name) — только в них имеет смысл передавать."""
    from apps.crm.models import ServiceCommonStatus
    return list(
        ServiceCommonStatus.objects
        .filter(service_name=service.name, is_active=True, department__isnull=False)
        .values_list("department_id", flat=True).distinct()
    )


def entry_common_status(service, department):
    """Входной (первый по order) общий статус отдела для услуги."""
    from apps.crm.models import ServiceCommonStatus
    return (
        ServiceCommonStatus.objects
        .filter(service_name=service.name, department=department, is_active=True)
        .order_by("order", "name").first()
    )


@transaction.atomic
def transfer_service(service, *, target_department=None, target_employee=None,
                     actor=None, keep_actor=False, comment=""):
    """Передать услугу в отдел или сотруднику. Возвращает список получателей.

    keep_actor=True («У меня завершить» снята) — актор оставляет услугу у себя
    (его ServiceEmployeeState не снимается), остальные прежние исполнители
    снимаются. keep_actor=False (галочка стоит) — полный переезд.

    comment — пояснение «что делать дальше», попадает в событийку.

    Бросает ValueError с понятным сообщением, если передать нельзя.
    """
    from apps.crm.models import Employee, ServiceEmployeeState, ServiceLog

    if target_employee is not None:
        department = target_employee.department
        if department is None:
            raise ValueError("У выбранного сотрудника не задан отдел.")
        recipients = [target_employee]
    elif target_department is not None:
        department = target_department
        recipients = list(
            Employee.objects.filter(is_active=True, department=department)
        )
        if not recipients:
            raise ValueError(f"В отделе «{department.name}» нет активных сотрудников.")
    else:
        raise ValueError("Не указан получатель передачи.")

    entry = entry_common_status(service, department)
    if entry is None:
        raise ValueError(
            f"Отдел «{department.name}» не ведёт услугу «{service.name.short_name}» "
            f"(нет общих статусов)."
        )

    old_status = service.common_status

    # Переезд: снимаем прежних исполнителей. Если «У меня завершить» снята
    # (keep_actor=True) — актор оставляет услугу у себя: его state сохраняем,
    # остальных прежних снимаем.
    keep_emp_id = actor.pk if (keep_actor and actor is not None) else None
    service.employee_states.exclude(employee_id=keep_emp_id).delete()
    leftover_ids = list(service.employee_states.values_list("employee_id", flat=True))
    service.employees.set(leftover_ids)

    # Меняем общий статус (этим услуга «переходит» в отдел).
    service.common_status = entry
    service.save(update_fields=["common_status"])

    # Новые получатели — в инбокс «Не принято». Кто уже остался исполнителем
    # (актор без завершения) — не трогаем.
    for emp in recipients:
        if emp.pk in leftover_ids:
            continue
        inbox = ensure_inbox_status(emp)
        ServiceEmployeeState.objects.create(
            service=service, employee=emp, status=inbox, updated_by=actor,
        )
        service.employees.add(emp)

    # Лог СОБЫТИЙКИ: действие + событие. Комментарий «что делать дальше» и
    # пометку «оставил у себя» подмешиваем в текст.
    sn = service.name.short_name
    note = f" — {comment}" if comment else ""
    self_note = " (исполнитель оставил услугу у себя)" if keep_emp_id else ""
    if target_employee is not None:
        action_comment = f"Услуга «{sn}» передана в работу сотруднику: {target_employee}{self_note}{note}"
        event_code = "employee_assigned"
        event_comment = (
            f"Услуга «{sn}» передана в работу — {target_employee} "
            f"(отдел «{department.name}», статус «{entry.name}»){note}"
        )
    else:
        action_comment = f"Услуга «{sn}» передана в работу отдела «{department.name}»{self_note}{note}"
        event_code = "dept_assigned"
        event_comment = (
            f"Услуга «{sn}» передана в работу отдела «{department.name}» "
            f"(статус «{entry.name}»), получателей: {len(recipients)}{note}"
        )

    action = client_log.record_action(
        service.client, "service_transfer", comment=action_comment, employee=actor,
    )
    client_log.record_event(
        service.client, event_code, comment=event_comment, employee=actor, parent=action,
    )
    ServiceLog.objects.create(
        service=service, employee=actor, action="common_status_change",
        old_common_status=old_status, new_common_status=entry,
        comment=action_comment,
    )
    return recipients
