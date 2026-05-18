"""Celery-задачи финансового модуля."""
from celery import shared_task

from .services import mark_overdue


@shared_task(name="apps.finance.tasks.mark_overdue_charges")
def mark_overdue_charges():
    """Дублирует логику management-команды для запуска из beat-расписания."""
    return mark_overdue()
