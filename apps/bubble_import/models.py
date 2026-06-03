"""Staging-модели и job-модель для импорта данных из исходной CRM на bubble.io.

Импорт идёт в два шага:
  1. FETCH  — постранично выкачиваем объекты из Bubble Data API в BubbleRecord
             (сырой JSON + извлечённые ключевые поля для UI).
  2. APPLY  — одобренные (``approved=True``) записи переносим в продакшн-модели
             SiriCRM (Client, Service, Payment/Charge, Message, StoredFile).

Generic-таблица BubbleRecord хранит все типы — различаются по полю ``entity``.
Идемпотентность: повторный fetch обновляет существующую запись по
(entity, bubble_id); повторный apply находит цель по ``target_*`` или по
``bubble_id`` на самой продакшн-модели.
"""
import uuid

from django.db import models


# Типы сущностей Bubble, которые импортируем в этой итерации.
ENTITY_CHOICES = [
    ("User", "Сотрудники (User)"),
    ("Man", "Клиенты (Man)"),
    ("ProjectBFL", "Услуги БФЛ (ProjectBFL)"),
    ("Money", "Платежи и начисления (Money)"),
    ("MessageWSP", "Сообщения WhatsApp (MessageWSP)"),
    ("Files", "Файлы (Files)"),
    ("Organization", "Юрлица (Organization → LegalEntity)"),
    ("Kreditors", "Кредиторы клиента (Kreditors → Kreditor)"),
    ("PropetyAnketa", "Имущество по анкете (PropetyAnketa → Answer.property_assets)"),
    ("Events", "События по услуге (Events → ClientEvent)"),
    # Внимание: первая буква — кириллическая «с» (U+0421/U+0441), это
    # особенность Bubble-схемы. Имя сущности должно совпадать с meta.
    ("Сorrespondence", "Корреспонденция (Сorrespondence → Correspondence)"),
]

STATUS_CHOICES = [
    ("pending", "Ожидает"),
    ("imported", "Импортирован"),
    ("skipped", "Пропущен"),
    ("error", "Ошибка"),
]


class BubbleFetchState(models.Model):
    """Курсор постраничной выгрузки по каждой сущности.

    Bubble Data API отдаёт по 100 объектов на запрос; cursor — смещение.
    Храним его, чтобы «Загрузить следующие N» продолжало с нужного места.
    """
    entity = models.CharField("Сущность", max_length=32, choices=ENTITY_CHOICES, unique=True)
    cursor = models.PositiveIntegerField("Текущий курсор", default=0)
    total_remote = models.PositiveIntegerField("Всего в Bubble", default=0)
    total_fetched = models.PositiveIntegerField("Выкачано в staging", default=0)
    last_fetch_at = models.DateTimeField("Последняя выгрузка", null=True, blank=True)

    class Meta:
        verbose_name = "Состояние выгрузки Bubble"
        verbose_name_plural = "Состояния выгрузки Bubble"

    def __str__(self):
        return f"{self.entity}: {self.total_fetched}/{self.total_remote}"


class BubbleRecord(models.Model):
    """Одна сырая запись из Bubble + статус её импорта в SiriCRM."""

    entity = models.CharField("Сущность", max_length=32, choices=ENTITY_CHOICES, db_index=True)
    bubble_id = models.CharField("Bubble _id", max_length=64, db_index=True)

    raw = models.JSONField("Сырой объект Bubble", default=dict)
    # Правки оператора в UI до Apply (перекрывают значения из raw).
    overrides = models.JSONField("Правки оператора", default=dict, blank=True)

    # Извлечённые поля для таблицы/фильтров UI (заполняются при fetch).
    display_title = models.CharField("Заголовок", max_length=300, blank=True, default="")
    display_subtitle = models.CharField("Подзаголовок", max_length=300, blank=True, default="")
    display_status = models.CharField(
        "Статус из Bubble", max_length=120, blank=True, default="",
        help_text="Напр. статус услуги (statusPrj) — для отображения в аудите.",
    )
    bubble_created = models.DateTimeField("Создан в Bubble", null=True, blank=True)

    # Управление импортом.
    approved = models.BooleanField("Одобрен к импорту", default=False)
    status = models.CharField(
        "Статус импорта", max_length=16, choices=STATUS_CHOICES, default="pending",
        db_index=True,
    )
    target_type = models.CharField("Модель-цель", max_length=32, blank=True, default="")
    target_id = models.CharField("ID созданного объекта", max_length=64, blank=True, default="")
    error = models.TextField("Текст ошибки", blank=True, default="")

    fetched_at = models.DateTimeField("Выгружен", auto_now=True)
    imported_at = models.DateTimeField("Импортирован", null=True, blank=True)

    class Meta:
        verbose_name = "Запись импорта Bubble"
        verbose_name_plural = "Записи импорта Bubble"
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "bubble_id"], name="uniq_bubble_entity_id",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "approved"]),
        ]
        ordering = ["entity", "bubble_created"]

    def __str__(self):
        return f"{self.entity}/{self.bubble_id} — {self.display_title}"

    def value(self, key, default=None):
        """Актуальное значение поля: override приоритетнее raw."""
        if key in (self.overrides or {}):
            return self.overrides[key]
        return (self.raw or {}).get(key, default)


JOB_STATUS_CHOICES = [
    ("pending", "Ожидает"),
    ("running", "Выполняется"),
    ("done", "Завершено"),
    ("error", "Ошибка"),
    ("cancelled", "Отменено"),
]


class BubbleImportJob(models.Model):
    """Фоновая задача массового импорта сущности из Bubble.

    Прогресс пишется celery-таской, UI поллит этот объект каждые ~2с
    через HTMX. На сущность одновременно — одна running задача.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity = models.CharField("Сущность", max_length=32, choices=ENTITY_CHOICES)
    status = models.CharField(
        "Статус", max_length=12, choices=JOB_STATUS_CHOICES, default="pending",
        db_index=True,
    )

    started_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bubble_import_jobs",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    current_action = models.CharField(
        "Текущее действие", max_length=200, blank=True, default="",
    )
    fetched_total = models.PositiveIntegerField("Выгружено всего", default=0)
    applied_count = models.PositiveIntegerField("Импортировано", default=0)
    errors_count = models.PositiveIntegerField("Ошибки", default=0)
    skipped_count = models.PositiveIntegerField("Пропущено", default=0)
    remote_total = models.PositiveIntegerField("Всего в Bubble", default=0)

    log_text = models.TextField("Лог (последние строки)", blank=True, default="")
    error_text = models.TextField("Trace при ошибке", blank=True, default="")

    cancel_requested = models.BooleanField(default=False)
    celery_task_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "Задача импорта Bubble"
        verbose_name_plural = "Задачи импорта Bubble"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["entity", "status"])]

    def __str__(self):
        return f"{self.entity}: {self.get_status_display()}"

    @property
    def is_running(self) -> bool:
        return self.status in ("pending", "running")

    @property
    def duration_sec(self) -> int:
        from django.utils import timezone
        end = self.finished_at or timezone.now()
        return int((end - self.started_at).total_seconds())

    def add_log(self, message: str, *, save: bool = True):
        """Дописать строку в лог (хранится последние ~50 строк)."""
        from django.utils import timezone
        ts = timezone.localtime().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        lines = (self.log_text or "").split("\n") if self.log_text else []
        lines.append(line)
        self.log_text = "\n".join(lines[-50:])
        if save:
            self.save(update_fields=["log_text"])
