"""Модели мониторинга арбитражных дел через kad.arbitr.ru.

Бизнес-сценарий:
  1) Сотрудник в карточке услуги БФЛ ставит «Иск отправлен в суд» →
     создаётся ArbitrCase(status='searching'), celery-таска ищет
     карточку клиента на kad по ФИО + коду суда (например, А12).
  2) Когда поиск нашёл подходящую карточку — сотрудник в UI подтверждает,
     вписывает case_number и kad_url → status переходит в 'monitoring',
     дальше celery-таска парсит страницу дела, добавляет новые события.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class ArbitrCase(models.Model):
    """Арбитражное дело клиента. Один к одному с услугой БФЛ."""
    STATUS_SEARCHING = "searching"
    STATUS_MONITORING = "monitoring"
    STATUS_CLOSED = "closed"
    STATUS_PAUSED = "paused"
    STATUS_CHOICES = [
        (STATUS_SEARCHING, "Ищем дело по ФИО"),
        (STATUS_MONITORING, "Мониторинг дела"),
        (STATUS_PAUSED, "Приостановлен"),
        (STATUS_CLOSED, "Завершено"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.OneToOneField(
        "crm.Service",
        on_delete=models.CASCADE,
        related_name="arbitr_case",
        verbose_name="Услуга",
    )
    status = models.CharField(
        "Статус мониторинга", max_length=20,
        choices=STATUS_CHOICES, default=STATUS_SEARCHING,
    )

    # Заполняется при подтверждении сотрудника (после нахождения карточки).
    case_number = models.CharField(
        "Номер дела", max_length=64, blank=True,
        help_text="Например: А12-1234/2026",
    )
    kad_url = models.URLField(
        "Ссылка на дело kad.arbitr.ru", max_length=500, blank=True,
    )
    court_name = models.CharField(
        "Суд (по факту)", max_length=255, blank=True,
        help_text="Заполняется парсером со страницы дела",
    )
    judge = models.CharField(
        "Судья", max_length=255, blank=True,
        help_text="Заполняется парсером",
    )
    instances = models.JSONField(
        "Инстанции", default=list, blank=True,
        help_text="Перечень инстанций где идёт спор (заполняется парсером)",
    )

    # Кто запустил мониторинг — этому сотруднику падают сообщения о капче
    # и подтверждение найденной карточки.
    started_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="started_arbitr_cases",
        verbose_name="Поставил «иск отправлен»",
    )

    # Тайминги мониторинга.
    last_check_at = models.DateTimeField(
        "Последняя проверка", null=True, blank=True,
    )
    last_check_ok = models.BooleanField(
        "Последняя проверка успешна", default=False,
    )
    last_error = models.TextField("Последняя ошибка", blank=True)

    # Найденные кандидаты на этапе SEARCHING (последний поиск по ФИО).
    # Каждый элемент — dict {case_number, kad_url, court_name, parties, filed_at}.
    # Сотрудник выбирает «своё» дело в UI → confirm_hit → case переходит в MONITORING,
    # список очищается.
    search_hits = models.JSONField(
        "Найденные кандидаты", default=list, blank=True,
    )
    search_hits_at = models.DateTimeField(
        "Когда найдены", null=True, blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Арбитражное дело"
        verbose_name_plural = "Арбитражные дела"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["last_check_at"]),
        ]

    def __str__(self):
        ident = self.case_number or "(без номера)"
        return f"{ident} · {self.get_status_display()}"


class ArbitrEvent(models.Model):
    """Событие/документ в карточке дела на kad.

    Идемпотентность — по (case, kad_event_id) либо (case, date, title).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        ArbitrCase,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name="Дело",
    )
    kad_event_id = models.CharField(
        "ID события на kad", max_length=128, blank=True,
        help_text="Если kad даёт стабильный идентификатор",
    )
    event_date = models.DateField("Дата события", null=True, blank=True)
    kind = models.CharField(
        "Тип", max_length=128, blank=True,
        help_text="Определение / решение / постановление / уведомление / иное",
    )
    title = models.CharField("Заголовок", max_length=500)
    description = models.TextField("Описание", blank=True)
    raw = models.JSONField(
        "Сырые поля с kad", default=dict, blank=True,
    )
    parsed_at = models.DateTimeField("Парсинг", auto_now_add=True)

    class Meta:
        verbose_name = "Событие дела"
        verbose_name_plural = "События дел"
        ordering = ["-event_date", "-parsed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "kad_event_id"],
                condition=models.Q(kad_event_id__gt=""),
                name="uniq_arbitr_event_kad_id",
            ),
        ]
        indexes = [
            models.Index(fields=["case", "event_date"]),
        ]

    def __str__(self):
        return f"{self.event_date or '—'} · {self.title[:80]}"


class ArbitrAttachment(models.Model):
    """Файл, прикреплённый к событию (определение, решение и т. п.)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(
        ArbitrEvent,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="Событие",
    )
    stored_file = models.ForeignKey(
        "files.StoredFile",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="arbitr_attachments",
        verbose_name="Файл в S3",
    )
    name = models.CharField("Имя файла", max_length=500)
    kad_url = models.URLField(
        "Ссылка на скачивание (kad)", max_length=1000, blank=True,
    )
    is_locked = models.BooleanField(
        "Закрытый файл (требует ЭЦП)", default=False,
        help_text="Если так — файл не скачивался, доступ по запросу",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Документ дела"
        verbose_name_plural = "Документы дел"

    def __str__(self):
        return self.name


class ArbitrCheckLog(models.Model):
    """Технический лог попыток мониторинга (для отладки парсера)."""
    STATE_OK = "ok"
    STATE_NOTHING = "nothing"
    STATE_ERROR = "error"
    STATE_CAPTCHA = "captcha"
    STATE_CHOICES = [
        (STATE_OK, "Успех"),
        (STATE_NOTHING, "Изменений нет"),
        (STATE_ERROR, "Ошибка парсера"),
        (STATE_CAPTCHA, "Капча"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        ArbitrCase,
        on_delete=models.CASCADE,
        related_name="check_logs",
        verbose_name="Дело",
    )
    ts = models.DateTimeField("Время", auto_now_add=True)
    state = models.CharField("Результат", max_length=16, choices=STATE_CHOICES)
    duration_ms = models.PositiveIntegerField("Длительность, мс", default=0)
    notes = models.TextField("Заметки/ошибка", blank=True)

    class Meta:
        verbose_name = "Лог проверки дела"
        verbose_name_plural = "Логи проверок дел"
        ordering = ["-ts"]

    def __str__(self):
        return f"{self.ts:%Y-%m-%d %H:%M} · {self.get_state_display()}"
