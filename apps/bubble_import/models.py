"""Staging-модели для импорта данных из исходной CRM на bubble.io.

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
from django.db import models


# Типы сущностей Bubble, которые импортируем в этой итерации.
ENTITY_CHOICES = [
    ("User", "Сотрудники (User)"),
    ("Man", "Клиенты (Man)"),
    ("ProjectBFL", "Услуги БФЛ (ProjectBFL)"),
    ("Money", "Платежи и начисления (Money)"),
    ("MessageWSP", "Сообщения WhatsApp (MessageWSP)"),
    ("Files", "Файлы (Files)"),
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
