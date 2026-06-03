"""Сигналы финансового модуля.

* После сохранения / удаления Payment с FK на Charge — пересчитываем
  Charge.status (paid / scheduled). Overdue в БД не пишем — это видимое
  значение из display_status / отдельной management-команды.
"""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Charge, Payment


@receiver(post_save, sender=Payment)
def _payment_saved(sender, instance: Payment, **kwargs):
    if instance.charge_id:
        instance.charge.recalc_status()


@receiver(post_delete, sender=Payment)
def _payment_deleted(sender, instance: Payment, **kwargs):
    if instance.charge_id:
        try:
            Charge.objects.get(pk=instance.charge_id).recalc_status()
        except Charge.DoesNotExist:
            pass
