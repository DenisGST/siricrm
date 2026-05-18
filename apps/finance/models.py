"""
Финансовый учёт: платежи (Payment), начисления (Charge) и справочники.

Справочники типов привязаны к ServiceName (в разрезе услуги).
Справочники касс — два разных (приход/расход), даже если по содержанию
могут пересекаться: ТЗ требует разделения.
"""
import uuid

from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        abstract = True


# === Справочники типов (в разрезе услуги) ===

class ExpenseType(TimeStampedModel):
    """Справочник типов расходов в разрезе услуги."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service_name = models.ForeignKey(
        "crm.ServiceName",
        on_delete=models.CASCADE,
        related_name="expense_types",
        verbose_name="Услуга",
    )
    name = models.CharField("Наименование", max_length=255)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Тип расхода"
        verbose_name_plural = "Типы расходов"
        ordering = ["service_name__short_name", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_name", "name"],
                name="unique_expense_type_per_service",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.service_name.short_name})"


class IncomeType(TimeStampedModel):
    """Справочник типов доходов в разрезе услуги."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service_name = models.ForeignKey(
        "crm.ServiceName",
        on_delete=models.CASCADE,
        related_name="income_types",
        verbose_name="Услуга",
    )
    name = models.CharField("Наименование", max_length=255)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Тип дохода"
        verbose_name_plural = "Типы доходов"
        ordering = ["service_name__short_name", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_name", "name"],
                name="unique_income_type_per_service",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.service_name.short_name})"


# === Справочники касс/банков ===

ACCOUNT_TYPE_CHOICES = [
    ("cash", "Касса"),
    ("bank", "Банк"),
]


class IncomingAccount(TimeStampedModel):
    """Куда поступил платёж (касса/банк + наименование)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_type = models.CharField("Тип", max_length=10, choices=ACCOUNT_TYPE_CHOICES)
    name = models.CharField("Наименование", max_length=255)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Счёт прихода"
        verbose_name_plural = "Счета прихода"
        ordering = ["account_type", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["account_type", "name"],
                name="unique_incoming_account",
            ),
        ]

    def __str__(self):
        return f"{self.get_account_type_display()}: {self.name}"


class OutgoingAccount(TimeStampedModel):
    """Откуда произведён платёж (касса/банк + наименование)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_type = models.CharField("Тип", max_length=10, choices=ACCOUNT_TYPE_CHOICES)
    name = models.CharField("Наименование", max_length=255)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Счёт расхода"
        verbose_name_plural = "Счета расхода"
        ordering = ["account_type", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["account_type", "name"],
                name="unique_outgoing_account",
            ),
        ]

    def __str__(self):
        return f"{self.get_account_type_display()}: {self.name}"


# === Начисления и платежи ===

CHARGE_STATUS_CHOICES = [
    ("scheduled", "В графике"),
    ("overdue", "Просрочен"),
    ("paid", "Оплачен"),
]


class Charge(TimeStampedModel):
    """Начисление: выставленный счёт / плановый платёж по графику рассрочки."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "crm.Client",
        on_delete=models.CASCADE,
        related_name="charges",
        verbose_name="Клиент",
    )
    service = models.ForeignKey(
        "crm.Service",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="charges",
        verbose_name="Услуга",
    )
    due_date = models.DateField("Дата платежа по графику")
    title = models.CharField("Наименование платежа", max_length=255)
    amount = models.DecimalField("Сумма начисления", max_digits=14, decimal_places=2)
    status = models.CharField(
        "Статус",
        max_length=20,
        choices=CHARGE_STATUS_CHOICES,
        default="scheduled",
    )
    comments = models.TextField("Комментарии", blank=True)

    created_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="created_charges",
        verbose_name="Кто внёс сведения",
    )
    updated_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="updated_charges",
        verbose_name="Кто отредактировал",
    )

    class Meta:
        verbose_name = "Начисление"
        verbose_name_plural = "Начисления"
        ordering = ["due_date"]

    def __str__(self):
        return f"{self.title} {self.amount} ₽ ({self.due_date})"

    @property
    def paid_amount(self):
        """Сумма всех привязанных к этому начислению входящих платежей."""
        from django.db.models import Sum
        agg = self.payments.filter(direction="in").aggregate(s=Sum("amount_in"))
        return agg["s"] or 0

    @property
    def remaining(self):
        return max(self.amount - self.paid_amount, 0)

    @property
    def display_status(self):
        """Видимый статус: paid если погашено, overdue если просрочка, иначе status поля."""
        import datetime
        if self.paid_amount >= self.amount:
            return "paid"
        if self.due_date and self.due_date < datetime.date.today():
            return "overdue"
        return self.status if self.status != "overdue" else "scheduled"

    def recalc_status(self, save=True):
        """Пересчитать поле status исходя из платежей. overdue не выставляем
        здесь — это делает management-команда / display_status. Возвращает
        новое значение."""
        new = "paid" if self.paid_amount >= self.amount else "scheduled"
        if new != self.status:
            self.status = new
            if save:
                self.save(update_fields=["status"])
        return new


DIRECTION_CHOICES = [
    ("in", "Входящий"),
    ("out", "Исходящий"),
]

PAYMENT_FORM_CHOICES = [
    ("cash", "Наличный"),
    ("cashless", "Безналичный"),
]


class Payment(TimeStampedModel):
    """Платёж: входящий (доход) или исходящий (расход)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    payment_date = models.DateField("Дата платежа")
    direction = models.CharField(
        "Тип платежа", max_length=10, choices=DIRECTION_CHOICES,
    )

    expense_type = models.ForeignKey(
        ExpenseType,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="payments",
        verbose_name="Тип расхода",
    )
    income_type = models.ForeignKey(
        IncomeType,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="payments",
        verbose_name="Тип дохода",
    )

    amount_in = models.DecimalField(
        "Сумма входящего", max_digits=14, decimal_places=2,
        null=True, blank=True,
    )
    amount_out = models.DecimalField(
        "Сумма исходящего", max_digits=14, decimal_places=2,
        null=True, blank=True,
    )

    payment_form = models.CharField(
        "Форма оплаты", max_length=10, choices=PAYMENT_FORM_CHOICES,
    )

    incoming_account = models.ForeignKey(
        IncomingAccount,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="payments",
        verbose_name="Куда поступил",
    )
    outgoing_account = models.ForeignKey(
        OutgoingAccount,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="payments",
        verbose_name="Откуда оплачено",
    )

    client = models.ForeignKey(
        "crm.Client",
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name="Клиент",
    )
    service = models.ForeignKey(
        "crm.Service",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="payments",
        verbose_name="Услуга",
    )
    charge = models.ForeignKey(
        Charge,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="payments",
        verbose_name="Платёж по графику",
    )

    comments = models.TextField("Комментарии", blank=True)

    created_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="created_payments",
        verbose_name="Кто внёс сведения",
    )
    updated_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="updated_payments",
        verbose_name="Кто отредактировал",
    )

    class Meta:
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"
        ordering = ["-payment_date", "-created_at"]
        indexes = [
            models.Index(fields=["client", "-payment_date"]),
        ]

    def __str__(self):
        amount = self.amount_in if self.direction == "in" else self.amount_out
        return f"{self.get_direction_display()} {amount or 0} ₽ ({self.payment_date})"
