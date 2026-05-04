import uuid
from django.db import models


class ConsultationResult(models.Model):
    """Справочник: итог консультации."""
    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name      = models.CharField("Наименование", max_length=100)
    color     = models.CharField("Цвет (daisyUI badge)", max_length=40, default="badge-neutral",
                                 help_text="badge-success, badge-error, badge-warning, badge-info и т.д.")
    order     = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Итог консультации"
        verbose_name_plural = "Итоги консультаций"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Consultation(models.Model):
    STATUS_CHOICES = [
        ("free",        "Свободно"),
        ("booked",      "Записан"),
        ("done",        "Проведена"),
        ("cancelled",   "Отменена"),
        ("transferred", "Перенесена"),
    ]

    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    consultant       = models.ForeignKey(
        "core.Employee", on_delete=models.CASCADE,
        related_name="consultations_as_consultant",
        verbose_name="Консультант",
    )
    client           = models.ForeignKey(
        "crm.Client", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="consultations",
        verbose_name="Клиент",
    )
    booked_by        = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="consultations_booked",
        verbose_name="Записал",
    )
    datetime_start   = models.DateTimeField("Начало")
    duration_minutes = models.PositiveIntegerField("Длительность (мин)", default=60)
    status           = models.CharField("Статус", max_length=20,
                                        choices=STATUS_CHOICES, default="free")
    result           = models.ForeignKey(
        ConsultationResult, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="consultations",
        verbose_name="Итог",
    )
    comment          = models.TextField("Комментарий оператора", blank=True)
    consultant_notes = models.TextField("Заметки консультанта", blank=True)
    transfer_reason  = models.TextField("Причина переноса", blank=True)
    transferred_to   = models.OneToOneField(
        "self", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="transferred_from",
        verbose_name="Перенесена в",
    )
    created_at       = models.DateTimeField("Создано", auto_now_add=True)
    updated_at       = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Консультация"
        verbose_name_plural = "Консультации"
        ordering = ["datetime_start"]
        constraints = [
            models.UniqueConstraint(
                fields=["consultant", "datetime_start"],
                name="unique_consultation_per_consultant_time",
            ),
        ]

    def __str__(self):
        dt = self.datetime_start
        return f"{self.consultant} / {dt.strftime('%d.%m.%Y %H:%M')}"

    @property
    def datetime_end(self):
        from datetime import timedelta
        return self.datetime_start + timedelta(minutes=self.duration_minutes)
