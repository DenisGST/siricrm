"""Модели раздела «Отчёты».

Бюджет отдела продаж (отчёт «Отдел продаж»): за месяц считается по правилу
начисления на каждую операцию-поступление. «Рассчитать» заполняет строки
(SalesBudgetEntry.accrued = computed) и итоговое поле SalesBudget.budget_total;
строки «Начислено» дальше правятся вручную онлайн.
"""
import uuid

from django.db import models

from apps.core.models import TimeStampedModel


class SalesBudget(TimeStampedModel):
    """Бюджет отдела продаж за один месяц (заполняется кнопкой «Рассчитать»)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    month = models.DateField(
        "Месяц", unique=True, help_text="Первое число месяца (период отчёта).",
    )
    budget_total = models.DecimalField(
        "Бюджет отдела продаж", max_digits=14, decimal_places=2, default=0,
    )
    calculated_at = models.DateTimeField("Рассчитано", null=True, blank=True)
    calculated_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Кто рассчитал",
    )

    class Meta:
        verbose_name = "Бюджет отдела продаж"
        verbose_name_plural = "Бюджеты отдела продаж"
        ordering = ["-month"]

    def __str__(self):
        return f"Бюджет ОП {self.month:%m.%Y}: {self.budget_total} ₽"


class SalesBudgetEntry(TimeStampedModel):
    """Начисление в бюджет ОП по одной операции-поступлению (строке отчёта).

    `payment` — представитель операции (первый платёж группы, как в отчёте).
    `computed` — расчётное значение по правилу; `accrued` — фактически
    начисленное (редактируется онлайн в таблице отчёта).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    budget = models.ForeignKey(
        SalesBudget, on_delete=models.CASCADE, related_name="entries",
        verbose_name="Бюджет (месяц)",
    )
    payment = models.ForeignKey(
        "finance.Payment", on_delete=models.CASCADE, related_name="+",
        verbose_name="Платёж (представитель операции)",
    )
    computed = models.DecimalField(
        "Расчётное начисление", max_digits=12, decimal_places=2, default=0,
    )
    accrued = models.DecimalField(
        "Начислено в бюджет отдела продаж", max_digits=12, decimal_places=2, default=0,
    )

    class Meta:
        verbose_name = "Начисление в бюджет ОП"
        verbose_name_plural = "Начисления в бюджет ОП"
        constraints = [
            models.UniqueConstraint(
                fields=["budget", "payment"], name="uniq_salesbudget_entry",
            ),
        ]

    def __str__(self):
        return f"{self.payment_id}: {self.accrued} ₽"
