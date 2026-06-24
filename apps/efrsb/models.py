"""Модели интеграции с ЕФРСБ (fedresurs.ru).

Слой над разделом «Процедуры банкротства» (apps.procedure). Назначение:
  • EfrsbMessageType  — каталог типов сообщений ЕФРСБ (DRAFT, правится в Справочниках),
                        сопоставляет НАШЕ событие процедуры с типом read-API и шаблоном
                        текста (движок АФД).
  • EfrsbBankruptLink — кэш bankruptGuid должника (резолвится из ИНН/СНИЛС через
                        /v1/bankrupts), привязан 1:1 к делу.
  • EfrsbPublication  — единый реестр: НАШИ заготовки (origin=internal, генерируем текст)
                        и ОБНАРУЖЕННЫЕ в реестре сообщения/отчёты (origin=discovered,
                        из read-API). Дедуп по fedresurs_guid.
  • EfrsbPublicationFile — файлы, приложенные к сообщению/отчёту (из /files/archive → S3).

🛑 Креды/контур (demo|prod) — только в env (config.py / settings), НЕ в моделях.
🛑 Авто-публикация — отдельный сервис fedresurs (УКЭП + договор), здесь только задел
   (status `submitted` зарезервирован, шов в submission.py). См. CLAUDE.md / план.
"""
from __future__ import annotations

import uuid

from django.db import models

from apps.core.models import TimeStampedModel

# Виды процедур (для applicable_kinds) — зеркалят apps.procedure.
KIND_RESTRUCTURING = "restructuring"
KIND_REALIZATION = "realization"


class EfrsbMessageType(TimeStampedModel):
    """Каталог типов сообщений ЕФРСБ (DB-editable, DRAFT).

    `api_type` (+ `api_type_aliases`) — значения поля `type` read-API (Приложение 1
    спецификации), по которым матчим обнаруженные публикации к нашему событию.
    `template`/`isk_template` — шаблон текста для генератора (движок АФД).
    `deadline_*` — справочный срок публикации (контроль сроков делает milestone-движок
    процедуры, здесь — только для подсказок; см. план, раздел «Контроль сроков»).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField("Код", max_length=60, unique=True)
    name = models.CharField("Тип сообщения", max_length=255)
    description = models.TextField("Описание / список плейсхолдеров", blank=True)

    api_type = models.CharField(
        "Тип в API ЕФРСБ", max_length=64, blank=True,
        help_text="Значение поля `type` read-API (напр. Meeting, Auction, TradeResult). "
                  "Пусто — тип только для генерации текста, без матчинга входящих.",
    )
    api_type_aliases = models.JSONField(
        "Доп. типы API (алиасы)", default=list, blank=True,
        help_text="Список других кодов API, относящихся к этому событию "
                  "(напр. [\"Meeting2\"], [\"Auction2\", \"ChangeAuction\"]).",
    )
    KIND_CHOICES = [
        (KIND_RESTRUCTURING, "Реструктуризация долгов"),
        (KIND_REALIZATION, "Реализация имущества"),
    ]
    applicable_kinds = models.JSONField(
        "Применим к видам процедур", default=list, blank=True,
        help_text="Список видов процедур: restructuring / realization. "
                  "Пусто — применим к обоим.",
    )
    API_KIND_MESSAGE = "message"
    API_KIND_REPORT = "report"
    API_KIND_CHOICES = [
        (API_KIND_MESSAGE, "Сообщение (/v1/messages)"),
        (API_KIND_REPORT, "Отчёт (/v1/reports)"),
    ]
    api_kind = models.CharField(
        "Раздел API", max_length=16, choices=API_KIND_CHOICES, default=API_KIND_MESSAGE,
        help_text="В каком разделе ЕФРСБ публикуется/ищется: сообщения или отчёты.",
    )

    template = models.ForeignKey(
        "afd.DocumentTemplate", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Шаблон текста (.docx)",
    )
    isk_template = models.ForeignKey(
        "afd.IskTemplate", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Секционный шаблон (альтернатива .docx)",
    )

    deadline_base_key = models.CharField(
        "Базовая дата срока (якорь)", max_length=32, blank=True,
        help_text="Из набора BASE_DATE_KEY_CHOICES процедуры (справочно).",
    )
    deadline_offset_days = models.IntegerField(
        "Срок публикации, дней", default=0,
        help_text="Срок по 127-ФЗ = базовая дата + N дней (справочно).",
    )
    sets_efrsb_date = models.BooleanField(
        "«Вводное» — проставляет дату публикации ЕФРСБ", default=False,
        help_text="При обнаружении публикации этого типа автозаполнить "
                  "Procedure.publication_efrsb_date (если ещё пусто) и пересчитать "
                  "дедлайны мероприятий. 🛑 Какие типы «вводные» — подтвердить с АУ.",
    )

    order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активен", default=True)
    is_draft = models.BooleanField(
        "Черновик (не подтверждён)", default=True,
        help_text="Соответствие типу API / сроки подлежат подтверждению АУ. Бейдж в UI.",
    )

    class Meta:
        verbose_name = "Тип сообщения ЕФРСБ"
        verbose_name_plural = "Типы сообщений ЕФРСБ"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    @property
    def all_api_types(self) -> list[str]:
        """api_type + алиасы (для матчинга обнаруженных публикаций)."""
        out = []
        if self.api_type:
            out.append(self.api_type)
        for a in (self.api_type_aliases or []):
            if a and a not in out:
                out.append(a)
        return out

    def applies_to_kind(self, kind: str) -> bool:
        ak = self.applicable_kinds or []
        return (not ak) or (kind in ak)


class EfrsbBankruptLink(TimeStampedModel):
    """Кэш идентификатора должника в ЕФРСБ (bankruptGuid) для дела.

    bankruptGuid резолвится из ИНН/СНИЛС через /v1/bankrupts. Несколько кандидатов →
    сохраняем `candidates` и ждём ручного подтверждения сотрудником (как
    ArbitrCase.search_hits). Поля next_* — smart-throttle (паттерн ArbitrCase.next_*_at).
    """
    MATCH_INN = "inn"
    MATCH_SNILS = "snils"
    MATCH_MANUAL = "manual"
    MATCH_CHOICES = [
        (MATCH_INN, "по ИНН"),
        (MATCH_SNILS, "по СНИЛС"),
        (MATCH_MANUAL, "вручную"),
    ]
    CONF_AUTO = "auto"
    CONF_CONFIRMED = "confirmed"
    CONF_CHOICES = [
        (CONF_AUTO, "Автоматически (1 кандидат)"),
        (CONF_CONFIRMED, "Подтверждено сотрудником"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.OneToOneField(
        "procedure.BankruptcyCase", on_delete=models.CASCADE,
        related_name="efrsb_link", verbose_name="Дело",
    )
    bankrupt_guid = models.CharField("GUID должника в ЕФРСБ", max_length=64, blank=True, db_index=True)
    match_method = models.CharField("Способ резолва", max_length=16, choices=MATCH_CHOICES, blank=True)
    match_confidence = models.CharField("Достоверность", max_length=16, choices=CONF_CHOICES, blank=True)
    candidates = models.JSONField(
        "Кандидаты (мульти-хит)", default=list, blank=True,
        help_text="Сырые pageData из /v1/bankrupts при нескольких совпадениях — для ручного выбора.",
    )
    resolved_at = models.DateTimeField("Резолвлен в", null=True, blank=True)
    last_search_at = models.DateTimeField("Последний поиск должника", null=True, blank=True)
    next_search_at = models.DateTimeField("Следующий поиск не ранее", null=True, blank=True)
    last_sync_at = models.DateTimeField("Последняя выборка сообщений", null=True, blank=True)
    next_sync_at = models.DateTimeField("Следующая выборка не ранее", null=True, blank=True)
    last_error = models.TextField("Последняя ошибка", blank=True)

    class Meta:
        verbose_name = "Связка должника с ЕФРСБ"
        verbose_name_plural = "Связки должников с ЕФРСБ"

    def __str__(self):
        return f"ЕФРСБ-связка дела {self.case_id}: {self.bankrupt_guid or '—'}"

    @property
    def is_resolved(self) -> bool:
        return bool(self.bankrupt_guid)


class EfrsbPublication(TimeStampedModel):
    """Публикация ЕФРСБ — НАША заготовка (origin=internal) либо ОБНАРУЖЕННАЯ (discovered).

    Единая таблица: матчинг «нашего» с «опубликованным» сводится в одну строку.
    Дедуп обнаруженных — по fedresurs_guid (UniqueConstraint).
    """
    KIND_MESSAGE = "message"
    KIND_REPORT = "report"
    KIND_CHOICES = [
        (KIND_MESSAGE, "Сообщение"),
        (KIND_REPORT, "Отчёт"),
    ]
    ORIGIN_INTERNAL = "internal"
    ORIGIN_DISCOVERED = "discovered"
    ORIGIN_CHOICES = [
        (ORIGIN_INTERNAL, "Наша заготовка"),
        (ORIGIN_DISCOVERED, "Обнаружена в реестре"),
    ]
    STATUS_DRAFT = "draft"
    STATUS_GENERATED = "generated"
    STATUS_SUBMITTED = "submitted"   # зарезервировано под авто-публикацию (Phase B)
    STATUS_PUBLISHED = "published"
    STATUS_ANNULLED = "annulled"
    STATUS_LOCKED = "locked"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_GENERATED, "Текст сформирован"),
        (STATUS_SUBMITTED, "Отправлено на публикацию"),
        (STATUS_PUBLISHED, "Опубликовано"),
        (STATUS_ANNULLED, "Аннулировано"),
        (STATUS_LOCKED, "Заблокировано"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        "procedure.BankruptcyCase", on_delete=models.CASCADE,
        related_name="efrsb_publications", verbose_name="Дело",
    )
    procedure = models.ForeignKey(
        "procedure.Procedure", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="efrsb_publications", verbose_name="Процедура",
    )
    message_type = models.ForeignKey(
        EfrsbMessageType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="publications", verbose_name="Тип сообщения",
    )
    kind = models.CharField("Вид", max_length=16, choices=KIND_CHOICES, default=KIND_MESSAGE)
    origin = models.CharField("Источник", max_length=16, choices=ORIGIN_CHOICES, default=ORIGIN_INTERNAL)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    title = models.CharField("Заголовок", max_length=255, blank=True)

    # ── Наш контент (origin=internal) ──
    generated_text = models.TextField("Текст сообщения (для ручной публикации)", blank=True)
    content_docx = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Документ (.docx)",
    )
    content_pdf = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Документ (PDF)",
    )
    overrides = models.JSONField(
        "Доп. поля события", default=dict, blank=True,
        help_text="Введённые вручную поля события (дата собрания, перечень имущества и т.п.).",
    )
    generated_at = models.DateTimeField("Текст сформирован", null=True, blank=True)
    created_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Кто сформировал",
    )

    # ── Факт публикации (из read-API) ──
    fedresurs_guid = models.CharField("GUID в ЕФРСБ", max_length=64, blank=True, db_index=True)
    fedresurs_number = models.CharField("Номер в ЕФРСБ", max_length=64, blank=True)
    bankrupt_guid = models.CharField("GUID должника", max_length=64, blank=True)
    date_publish = models.DateTimeField("Дата публикации", null=True, blank=True)
    api_type = models.CharField("Тип API", max_length=64, blank=True)
    procedure_type = models.CharField("Тип процедуры (API)", max_length=40, blank=True)
    has_violation = models.BooleanField("Публикация с нарушением срока", null=True)
    is_annulled = models.BooleanField("Аннулировано", default=False)
    annulment_guid = models.CharField("GUID сообщения об аннулировании", max_length=64, blank=True)
    is_locked = models.BooleanField("Заблокировано", default=False)
    lock_reason = models.CharField("Причина блокировки", max_length=255, blank=True)
    linked_guids = models.JSONField("Связанные публикации (guid)", default=list, blank=True)
    content_xml = models.TextField("Контент (XML)", blank=True)
    raw = models.JSONField("Сырой ответ API", default=dict, blank=True)
    discovered_at = models.DateTimeField("Обнаружено в реестре", null=True, blank=True)
    matched_at = models.DateTimeField("Привязано к заготовке", null=True, blank=True)

    notes = models.TextField("Заметки", blank=True)

    class Meta:
        verbose_name = "Публикация ЕФРСБ"
        verbose_name_plural = "Публикации ЕФРСБ"
        ordering = ["-date_publish", "-created_at"]
        indexes = [
            models.Index(fields=["case", "status"]),
            models.Index(fields=["case", "kind"]),
            models.Index(fields=["date_publish"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["fedresurs_guid"],
                condition=models.Q(fedresurs_guid__gt=""),
                name="uniq_efrsb_pub_guid",
            ),
        ]

    def __str__(self):
        return self.title or f"Публикация {self.id}"

    @property
    def type_label(self) -> str:
        if self.message_type_id and self.message_type:
            return self.message_type.name
        return self.api_type or "—"

    @property
    def fedresurs_url(self) -> str:
        """Публичная ссылка на сообщение/отчёт на Федресурс (по guid)."""
        if not self.fedresurs_guid:
            return ""
        base = "https://fedresurs.ru/messages/" if self.kind == self.KIND_MESSAGE \
            else "https://fedresurs.ru/reports/"
        return f"{base}{self.fedresurs_guid}"


class EfrsbPublicationFile(TimeStampedModel):
    """Файл, приложенный к сообщению/отчёту ЕФРСБ (скачан из /files/archive в S3)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(
        EfrsbPublication, on_delete=models.CASCADE,
        related_name="files", verbose_name="Публикация",
    )
    stored_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Файл в S3",
    )
    name = models.CharField("Имя файла", max_length=500)
    is_safe = models.BooleanField("Безопасный (прошёл антивирус)", default=True)

    class Meta:
        verbose_name = "Файл публикации ЕФРСБ"
        verbose_name_plural = "Файлы публикаций ЕФРСБ"
        ordering = ["name"]

    def __str__(self):
        return self.name
