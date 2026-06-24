"""Модель уведомлений сотрудникам (apps.notifications).

Концепция: запись событийки (`crm.ClientLogEntry`) с `notifies=True` в своём
справочнике (EventType/ActionType) порождает по строке `Notification` на каждого
сотрудника, который работает с клиентом (ориентир — «Мой канбан»; адресно, если
у события есть явный получатель). Уведомление живёт своим жизненным циклом:

    new → accepted (взял в работу) → done (исполнено)
    new → rejected (отклонил)
    new → snoozed (отложил, snooze_until) → new (по таймеру beat)

Реакция сотрудника (web или telegram) пишется обратно в событийку через
`crm.client_log.record_action` (см. apps/notifications/services.py).
"""
from django.db import models


class Notification(models.Model):
    STATUS_NEW = "new"
    STATUS_ACCEPTED = "accepted"
    STATUS_DONE = "done"
    STATUS_REJECTED = "rejected"
    STATUS_SNOOZED = "snoozed"
    STATUS_ACKNOWLEDGED = "acknowledged"
    STATUS_CHOICES = [
        (STATUS_NEW, "Новое"),
        (STATUS_ACCEPTED, "В работе"),
        (STATUS_DONE, "Исполнено"),
        (STATUS_REJECTED, "Отклонено"),
        (STATUS_SNOOZED, "Отложено"),
        (STATUS_ACKNOWLEDGED, "Ознакомлен"),
    ]
    # Закрытые статусы — не висят в активном списке и не светят бейдж.
    CLOSED_STATUSES = (STATUS_DONE, STATUS_REJECTED, STATUS_ACKNOWLEDGED)

    VIA_CHOICES = [("web", "Сайт"), ("telegram", "Telegram")]

    # ─── Кому. Фан-аут: одно событие → по строке на получателя. ───
    recipient = models.ForeignKey(
        "core.Employee", on_delete=models.CASCADE,
        related_name="notifications", verbose_name="Получатель",
    )
    client = models.ForeignKey(
        "crm.Client", on_delete=models.CASCADE,
        related_name="notifications", verbose_name="Клиент",
    )

    # ─── Откуда. Запись событийки, породившая уведомление. ───
    source = models.ForeignKey(
        "crm.ClientLogEntry", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="notifications",
        verbose_name="Запись событийки",
    )
    text = models.CharField(
        "Текст", max_length=500,
        help_text="Снимок текста уведомления (переживает изменение source).",
    )
    hint = models.CharField(
        "Подсказка-что-делать", max_length=255, blank=True,
    )

    # ─── Жизненный цикл. ───
    status = models.CharField(
        "Статус", max_length=12, choices=STATUS_CHOICES,
        default=STATUS_NEW, db_index=True,
    )
    snooze_until = models.DateTimeField("Отложено до", null=True, blank=True)

    # ─── Реакция. ───
    responded_at = models.DateTimeField("Время реакции", null=True, blank=True)
    responded_via = models.CharField(
        "Канал реакции", max_length=10, choices=VIA_CHOICES, blank=True,
    )
    response_log = models.ForeignKey(
        "crm.ClientLogEntry", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        verbose_name="Запись-ответ в событийке",
    )

    # Telegram-зеркало: id отправленного боту сообщения, чтобы редактировать
    # карточку при реакции с сайта (и наоборот).
    tg_message_id = models.BigIntegerField(null=True, blank=True)

    created_at = models.DateTimeField("Создано", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "status"]),
            models.Index(fields=["status", "snooze_until"]),
        ]

    def __str__(self):
        return f"{self.recipient_id} ← {self.text[:40]} [{self.status}]"

    @property
    def is_active(self) -> bool:
        """Активное (висит в списке): new / accepted / snoozed."""
        return self.status not in self.CLOSED_STATUSES
