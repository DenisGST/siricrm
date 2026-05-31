"""Инбокс «Не принято» в «Моём канбане».

Универсальная личная колонка сотрудника (ServiceEmployeeStatus с
is_inbox=True, common_status=None). Сюда попадают услуги, переданные
сотруднику/отделу через будущее событие «Передать в работу отдела/
сотрудника» и ещё не принятые в работу. Одна на сотрудника.
"""
import logging

logger = logging.getLogger(__name__)

INBOX_STATUS_NAME = "Не принято"
# Имена, которые при нормализации считаем тем же инбоксом.
_INBOX_ALIASES = {"не принято", "непринятые"}


def ensure_inbox_status(employee):
    """Гарантировать у сотрудника инбокс-статус «Не принято». Идемпотентно.

    Возвращает ServiceEmployeeStatus (is_inbox=True). Переиспользует уже
    существующий инбокс или статус с подходящим именем («Не принято» /
    «Непринятые»), нормализуя его (is_inbox=True, common_status=None).
    """
    from apps.crm.models import ServiceEmployeeStatus

    if employee is None:
        return None

    existing = list(ServiceEmployeeStatus.objects.filter(employee=employee))
    inbox = next((s for s in existing if s.is_inbox), None)
    if inbox is None:
        inbox = next(
            (s for s in existing if (s.name or "").strip().lower() in _INBOX_ALIASES),
            None,
        )
    if inbox is None:
        inbox = ServiceEmployeeStatus(employee=employee)

    inbox.name = INBOX_STATUS_NAME
    inbox.is_inbox = True
    inbox.common_status = None
    inbox.order = 0
    inbox.is_active = True
    inbox.save()
    return inbox
