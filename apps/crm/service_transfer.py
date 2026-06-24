"""Передача услуги в работу отдела/сотрудника.

Услуга просто кладётся в инбокс «Не принято» (ServiceEmployeeStatus.is_inbox)
получателя(ей). Общий статус услуги (Service.common_status) при передаче НЕ
меняется — эту логику пропишем отдельно. Передача сотруднику → ему одному;
в отдел → всем сотрудникам отдела.

Ограничения получателя: действующий (is_active) и работает с этой услугой
(Employee.services_allowed содержит ServiceName).

Логируется в СОБЫТИЙКЕ: ActionType `service_transfer` + событие
`dept_assigned` (в отдел) / `employee_assigned` (сотруднику).
"""
import logging

from django.db import transaction
from django.utils import timezone

from apps.crm import client_log
from apps.crm.kanban_inbox import ensure_inbox_status

logger = logging.getLogger(__name__)


def eligible_employees(service):
    """Сотрудники, которым можно передать услугу: действующие и работающие
    с этой услугой (services_allowed содержит service.name)."""
    from apps.crm.models import Employee
    return Employee.objects.filter(
        is_active=True, services_allowed=service.name,
    )


@transaction.atomic
def transfer_service(service, *, target_department=None, target_employee=None,
                     actor=None, keep_actor=False, comment=""):
    """Передать услугу в отдел или сотруднику. Возвращает список получателей.

    keep_actor=True («У меня завершить» снята) — актор оставляет услугу у себя
    (его ServiceEmployeeState не снимается), остальные прежние исполнители
    снимаются. keep_actor=False (галочка стоит) — полный переезд.

    comment — пояснение «что делать дальше», попадает в событийку.

    Получатели фильтруются eligible_employees (действующие + работают с услугой).
    Общий статус услуги НЕ меняется.

    Бросает ValueError с понятным сообщением, если передать нельзя.
    """
    from apps.crm.models import ServiceEmployeeState

    base = eligible_employees(service)
    if target_employee is not None:
        department = target_employee.department
        recipients = list(base.filter(pk=target_employee.pk))
        if not recipients:
            raise ValueError("Сотрудник не работает с этой услугой или не действующий.")
    elif target_department is not None:
        department = target_department
        recipients = list(base.filter(department=department))
        if not recipients:
            raise ValueError(
                f"В отделе «{department.name}» нет действующих сотрудников, "
                f"работающих с услугой «{service.name.short_name}»."
            )
    else:
        raise ValueError("Не указан получатель передачи.")

    # Снимаем прежних исполнителей. Если «У меня завершить» снята
    # (keep_actor=True) — актор оставляет услугу у себя: его state сохраняем,
    # остальных прежних снимаем.
    keep_emp_id = actor.pk if (keep_actor and actor is not None) else None
    service.employee_states.exclude(employee_id=keep_emp_id).delete()
    leftover_ids = list(service.employee_states.values_list("employee_id", flat=True))
    service.employees.set(leftover_ids)

    # Новые получатели — в инбокс «Не принято». Кто уже остался исполнителем
    # (актор без завершения) — не трогаем. Общий статус услуги НЕ меняем.
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
            f"Услуга «{sn}» передана в работу — {target_employee}{note}"
        )
    else:
        action_comment = f"Услуга «{sn}» передана в работу отдела «{department.name}»{self_note}{note}"
        event_code = "dept_assigned"
        event_comment = (
            f"Услуга «{sn}» передана в работу отдела «{department.name}», "
            f"получателей: {len(recipients)}{note}"
        )

    action = client_log.record_action(
        service.client, "service_transfer", comment=action_comment, employee=actor,
    )
    client_log.record_event(
        service.client, event_code, comment=event_comment, employee=actor, parent=action,
    )

    # Бизнес-логика: при первой передаче в отдел сбора документов — проставить
    # дату передачи в услугу (п.3 дат услуги в карточке процедуры).
    if getattr(department, "is_docs_collection", False) and not service.docs_dept_date:
        service.docs_dept_date = timezone.localdate()
        service.save(update_fields=["docs_dept_date"])

    return recipients
