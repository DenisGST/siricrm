"""Модели раздела «Бухгалтерский учёт».

`IncomingPayment` — буфер входящих платежей из двух источников (выписка р/с +
эквайринг). Бухгалтер вручную привязывает платёж к клиенту и (опц.) начислениям;
при привязке создаются записи `finance.Payment` (одна на начисление — сумму можно
разбить), а начисления гасятся через `Charge.paid_amount`.

`SourcePoll` — журнал опросов источников (для вкладки «Банк»).
"""
import uuid

from django.db import models

from apps.finance.models import TimeStampedModel


class IncomingPayment(TimeStampedModel):
    """Входящий платёж, ожидающий разнесения бухгалтером."""

    SOURCE_STATEMENT = "statement"
    SOURCE_ACQUIRING = "acquiring"
    SOURCE_CHOICES = [
        (SOURCE_STATEMENT, "Выписка р/с"),
        (SOURCE_ACQUIRING, "Эквайринг"),
    ]

    STATUS_NEW = "new"
    STATUS_BOUND = "bound"
    STATUS_UNIDENTIFIED = "unidentified"
    STATUS_CHOICES = [
        (STATUS_NEW, "Не привязан"),
        (STATUS_BOUND, "Привязан"),
        (STATUS_UNIDENTIFIED, "Неопознанный"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    source = models.CharField("Источник", max_length=16, choices=SOURCE_CHOICES)
    # ID операции в банке / PaymentId эквайринга — для дедупликации при поллинге.
    external_id = models.CharField("ID операции в источнике", max_length=128)

    occurred_at = models.DateTimeField("Дата/время платежа")
    amount = models.DecimalField("Сумма", max_digits=14, decimal_places=2)

    # Данные плательщика. Для выписки — реальный плательщик; для эквайринга —
    # введённые клиентом на странице оплаты ФИО/телефон (🛑 могут быть с ошибкой).
    payer_name = models.CharField("Плательщик / ФИО", max_length=255, blank=True)
    payer_inn = models.CharField("ИНН плательщика", max_length=16, blank=True)
    payer_phone = models.CharField("Телефон (введён клиентом)", max_length=32, blank=True)
    purpose = models.TextField("Назначение", blank=True)
    # OrderId эквайринга — задел под будущую авто-привязку (фаза 2).
    order_id = models.CharField("OrderId эквайринга", max_length=128, blank=True)

    status = models.CharField(
        "Статус", max_length=16, choices=STATUS_CHOICES,
        default=STATUS_NEW, db_index=True,
    )
    # Сводное/терминальное зачисление эквайринга в выписке (плательщик —
    # банк-эквайер, а не реальный клиент). Помечается, но не убирается из очереди.
    is_settlement = models.BooleanField("Эквайринг-зачисление", default=False, db_index=True)
    note = models.TextField("Комментарий бухгалтера", blank=True)

    raw = models.JSONField("Сырой ответ источника", default=dict, blank=True)

    bound_client = models.ForeignKey(
        "crm.Client", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="incoming_payments", verbose_name="Клиент",
    )
    bound_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bound_incoming_payments", verbose_name="Кто привязал",
    )
    bound_at = models.DateTimeField("Когда привязан", null=True, blank=True)
    # Созданные при привязке платежи (по одному на начисление; M2M, чтобы при
    # переразнесении («Изменить») можно было удалить и создать заново).
    created_payments = models.ManyToManyField(
        "finance.Payment", blank=True, related_name="+",
        verbose_name="Созданные платежи",
    )

    class Meta:
        verbose_name = "Входящий платёж"
        verbose_name_plural = "Входящие платежи"
        ordering = ["-occurred_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                name="unique_incoming_payment_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "source"]),
            models.Index(fields=["-occurred_at"]),
        ]

    def __str__(self):
        return f"{self.get_source_display()} {self.amount} ₽ ({self.occurred_at:%d.%m.%Y})"


class AcquiringPrepay(TimeStampedModel):
    """Данные, присланные страницей оплаты (fo-y.ru) ДО платежа — введённые
    клиентом ФИО/телефон, ключ — `order_id`. ТБанк эти поля в нотификации не
    возвращает, поэтому склеиваем нотификацию (по OrderId) с этой записью."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_id = models.CharField("OrderId", max_length=128, unique=True, db_index=True)
    name = models.CharField("ФИО (введено клиентом)", max_length=255, blank=True)
    phone = models.CharField("Телефон (введён клиентом)", max_length=32, blank=True)
    amount = models.DecimalField("Сумма", max_digits=14, decimal_places=2, null=True, blank=True)
    matched = models.BooleanField("Сопоставлен с платежом", default=False)

    class Meta:
        verbose_name = "Pre-pay эквайринга"
        verbose_name_plural = "Pre-pay эквайринга"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.order_id} — {self.name} {self.phone}"


class SourcePoll(TimeStampedModel):
    """Журнал опроса источника (для вкладки «Банк» — история/статус)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(
        "Источник", max_length=16, choices=IncomingPayment.SOURCE_CHOICES, db_index=True,
    )
    found = models.PositiveIntegerField("Найдено входящих", default=0)
    created = models.PositiveIntegerField("Новых", default=0)
    ok = models.BooleanField("Успех", default=True)
    error = models.TextField("Ошибка", blank=True)

    class Meta:
        verbose_name = "Опрос источника"
        verbose_name_plural = "Опросы источников"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_source_display()} @ {self.created_at:%d.%m %H:%M} (+{self.created})"
