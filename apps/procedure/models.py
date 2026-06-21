"""Модели раздела «Процедуры банкротства» — рабочее место помощников АУ.

Двухуровневая модель (домен БФЛ, 127-ФЗ):
  • Дело (BankruptcyCase) — 1:1 к услуге БФЛ. Несёт ОБЩИЕ стадии
    (Подготовка → Подача → Принятие судом / первое заседание) и итог первого
    заседания.
  • Процедура (Procedure) — одна или несколько внутри дела. У дела бывает
    сразу реализация, либо сначала реструктуризация, затем реализация. У каждой
    процедуры свои стадии, даты (определение/публикация) и мероприятия-сроки.

🛑 Сроки мероприятий — ДАННЫЕ в `MilestoneTemplate` (DB-editable), не хардкод.
Исходы первого заседания и процедур — фиксированные перечни (ниже), приходят
от АУ; терминальные исходы закрывают дело.
"""
from __future__ import annotations

import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.core.models import TimeStampedModel


# ── Виды процедур ──────────────────────────────────────────────────────────
KIND_RESTRUCTURING = "restructuring"
KIND_REALIZATION = "realization"
PROCEDURE_KIND_CHOICES = [
    (KIND_RESTRUCTURING, "Реструктуризация долгов"),
    (KIND_REALIZATION, "Реализация имущества"),
]

# ── Область стадии: общая или внутри процедуры конкретного вида ─────────────
SCOPE_COMMON = "common"
STAGE_KIND_SCOPE_CHOICES = [
    (SCOPE_COMMON, "Общая (стадии дела)"),
    (KIND_RESTRUCTURING, "Реструктуризация долгов"),
    (KIND_REALIZATION, "Реализация имущества"),
]

# ── Базовые даты — якоря для расчёта сроков мероприятий ─────────────────────
# Общие (case_*) резолвятся от полей дела, процедурные (proc_*) — от процедуры.
BASE_DATE_KEY_CHOICES = [
    ("case_filing_date", "Дата подачи иска в суд"),
    ("case_claim_accept_date", "Дата приёма иска в суде"),
    ("case_first_hearing_date", "Дата первого судебного заседания"),
    ("proc_intro_date", "Дата решения о введении процедуры"),
    ("proc_publication_efrsb_date", "Дата публикации в ЕФРСБ"),
    ("proc_publication_kommersant_date", "Дата публикации в КоммерсантЪ"),
]

# ── Исходы первого заседания (итог общей фазы дела) ─────────────────────────
FIRST_HEARING_OUTCOMES = [
    ("fh_refused", "Отказано во введении процедуры"),
    ("fh_intro_restructuring", "Введена процедура реструктуризации"),
    ("fh_intro_realization", "Введена процедура реализации имущества"),
    ("fh_settlement", "Мировое соглашение"),
]

# ── Исходы процедуры реструктуризации ──────────────────────────────────────
RESTRUCTURING_OUTCOMES = [
    ("restr_plan_approved", "Утверждён план реструктуризации"),
    ("restr_intro_realization", "Введена процедура реализации имущества"),
    ("restr_settlement", "Заключено мировое соглашение"),
    ("restr_terminated", "Прекращение процедуры"),
]

# ── Исходы процедуры реализации имущества ───────────────────────────────────
REALIZATION_OUTCOMES = [
    ("real_discharge_full", "Освобождение от обязательств (полное списание долгов)"),
    ("real_discharge_partial", "Частичное освобождение от обязательств (списано часть долгов)"),
    ("real_no_discharge", "Завершение процедуры без списания долгов"),
    ("real_settlement", "Заключено мировое соглашение"),
    ("real_proceedings_terminated", "Производство по делу прекращено"),
]

PROCEDURE_OUTCOME_CHOICES = RESTRUCTURING_OUTCOMES + REALIZATION_OUTCOMES
ALL_OUTCOMES = dict(FIRST_HEARING_OUTCOMES + PROCEDURE_OUTCOME_CHOICES)

# Исходы, закрывающие дело (терминальные). Остальные — дело продолжается
# (введена следующая процедура / план утверждён).
CLOSING_OUTCOMES = {
    "fh_refused", "fh_settlement",
    "restr_settlement", "restr_terminated",
    "real_discharge_full", "real_discharge_partial", "real_no_discharge",
    "real_settlement", "real_proceedings_terminated",
}


def outcomes_for_kind(kind: str):
    return RESTRUCTURING_OUTCOMES if kind == KIND_RESTRUCTURING else REALIZATION_OUTCOMES


class ProcedureStage(TimeStampedModel):
    """Каталог стадий (упорядоченный, редактируется в админке/UI).

    `kind_scope` помечает, к чему относится стадия: общие стадии дела или
    стадии процедуры конкретного вида.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField("Код", max_length=40, unique=True)
    name = models.CharField("Название", max_length=120)
    kind_scope = models.CharField(
        "Область", max_length=20,
        choices=STAGE_KIND_SCOPE_CHOICES, default=SCOPE_COMMON,
    )
    order = models.PositiveIntegerField("Порядок", default=0)
    is_terminal = models.BooleanField(
        "Завершающая", default=False, help_text="Стадия «Завершено».",
    )
    is_active = models.BooleanField("Активна", default=True)

    class Meta:
        verbose_name = "Стадия процедуры"
        verbose_name_plural = "Стадии процедур"
        ordering = ["order"]

    def __str__(self):
        return self.name


class MilestoneTemplate(TimeStampedModel):
    """Каталог обязательных мероприятий по стадиям (DB-editable, DRAFT)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stage = models.ForeignKey(
        ProcedureStage, on_delete=models.PROTECT,
        related_name="milestone_templates", verbose_name="Стадия",
    )
    code = models.SlugField("Код", max_length=60, unique=True)
    title = models.CharField("Мероприятие", max_length=255)
    description = models.TextField("Описание", blank=True)
    base_date_key = models.CharField(
        "Базовая дата (якорь срока)", max_length=32,
        choices=BASE_DATE_KEY_CHOICES, blank=True,
        help_text="От какой даты считать дедлайн. Пусто — без срока.",
    )
    offset_days = models.IntegerField(
        "Смещение, дней", default=0,
        help_text="Дедлайн = базовая дата + N дней (можно отрицательное).",
    )
    is_mandatory = models.BooleanField("Обязательное", default=True)
    responsible_role = models.CharField("Ответственная роль", max_length=20, blank=True)
    order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активно", default=True)
    is_draft = models.BooleanField(
        "Черновик (срок не подтверждён)", default=True,
        help_text="Состав/сроки подлежат подтверждению АУ. Бейдж в UI.",
    )

    class Meta:
        verbose_name = "Шаблон мероприятия"
        verbose_name_plural = "Шаблоны мероприятий"
        ordering = ["stage__order", "order"]

    def __str__(self):
        return self.title


class BankruptcyCase(TimeStampedModel):
    """Дело о банкротстве по услуге БФЛ (OneToOne к crm.Service).

    Несёт общие стадии и итог первого заседания. Процедуры — дочерние записи.
    """
    STATUS_ACTIVE = "active"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "В работе"),
        (STATUS_CLOSED, "Дело закрыто"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.OneToOneField(
        "crm.Service", on_delete=models.CASCADE,
        related_name="bankruptcy_case", verbose_name="Услуга",
    )
    status = models.CharField(
        "Статус дела", max_length=12, choices=STATUS_CHOICES, default=STATUS_ACTIVE,
    )
    # Текущее положение (подсвеченная стадия) + активная процедура.
    current_stage = models.ForeignKey(
        ProcedureStage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="current_cases", verbose_name="Текущая стадия",
    )
    current_procedure = models.ForeignKey(
        "procedure.Procedure", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="active_in_cases", verbose_name="Активная процедура",
    )
    # Финуправляющий — атрибут процедуры (назначается судом при введении).
    # В сводке дела показываем ФУ последней процедуры (см. fm_display).
    # Общие даты дела (якоря сроков общих стадий).
    filing_date = models.DateField("Дата подачи иска в суд", null=True, blank=True)
    claim_accept_date = models.DateField("Дата приёма иска в суде", null=True, blank=True)
    first_hearing_date = models.DateField("Дата первого судебного заседания", null=True, blank=True)
    first_hearing_outcome = models.CharField(
        "Итог первого заседания", max_length=32,
        choices=FIRST_HEARING_OUTCOMES, blank=True,
    )
    notes = models.TextField("Заметки", blank=True)

    class Meta:
        verbose_name = "Дело о банкротстве"
        verbose_name_plural = "Дела о банкротстве"

    def __str__(self):
        return f"Дело: {self.service}"

    @property
    def fm_display(self) -> str:
        """ФУ дела = ФУ последней (актуальной) процедуры."""
        last = self.procedures.order_by("-order").first()
        return last.fm_display if last else "—"

    @property
    def result_label(self) -> str:
        """Текстовый итог закрытого дела (по терминальному исходу)."""
        if self.status != self.STATUS_CLOSED:
            return ""
        # Берём исход последней процедуры, иначе итог первого заседания.
        last = self.procedures.exclude(outcome="").order_by("-order").first()
        code = (last.outcome if last and last.outcome else self.first_hearing_outcome)
        return ALL_OUTCOMES.get(code, "")


class Procedure(TimeStampedModel):
    """Процедура внутри дела (реструктуризация/реализация) со своими стадиями,
    датами и исходом."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        BankruptcyCase, on_delete=models.CASCADE,
        related_name="procedures", verbose_name="Дело",
    )
    kind = models.CharField("Вид процедуры", max_length=20, choices=PROCEDURE_KIND_CHOICES)
    order = models.PositiveIntegerField("Порядок", default=0)
    current_stage = models.ForeignKey(
        ProcedureStage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="current_procedures", verbose_name="Текущая стадия",
    )
    # ФУ назначается судом при введении этой процедуры.
    financial_manager = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="managed_procedures", verbose_name="Финуправляющий (штатный)",
    )
    fm_name_external = models.CharField(
        "Финуправляющий (внешний)", max_length=255, blank=True,
        help_text="ФИО АУ, если он не штатный сотрудник.",
    )
    # Даты процедуры (якоря сроков её мероприятий).
    intro_date = models.DateField(
        "Дата принятия решения о введении процедуры", null=True, blank=True,
    )
    publication_efrsb_date = models.DateField("Дата публикации в ЕФРСБ", null=True, blank=True)
    publication_kommersant_date = models.DateField("Дата публикации в КоммерсантЪ", null=True, blank=True)
    next_hearing_date = models.DateField("Дата следующего судебного заседания", null=True, blank=True)
    term_months = models.PositiveSmallIntegerField(
        "Срок процедуры, мес.", null=True, blank=True,
        help_text="Обычно от 4 до 6 месяцев.",
    )
    end_date = models.DateField(
        "Дата решения об окончании/завершении процедуры", null=True, blank=True,
    )
    outcome = models.CharField(
        "Исход процедуры", max_length=40, choices=PROCEDURE_OUTCOME_CHOICES, blank=True,
    )
    notes = models.TextField("Заметки", blank=True)

    class Meta:
        verbose_name = "Процедура"
        verbose_name_plural = "Процедуры"
        ordering = ["order"]

    def __str__(self):
        return f"{self.get_kind_display()} (дело {self.case_id})"

    @property
    def outcome_label(self) -> str:
        return ALL_OUTCOMES.get(self.outcome, "")

    @property
    def fm_display(self) -> str:
        e = self.financial_manager
        if e is not None:
            name = " ".join(filter(None, [e.user.last_name, e.user.first_name, e.patronymic]))
            return name.strip() or e.user.get_full_name() or e.user.username
        return self.fm_name_external or "—"


class ProcedureMilestone(TimeStampedModel):
    """Экземпляр мероприятия (со сроком и статусом).

    Принадлежит делу; `procedure` указывает на конкретную процедуру (null —
    мероприятие общей фазы дела). Поля title/base_date_key/offset_days —
    снапшот из шаблона, чтобы правка каталога не переписывала историю.
    """
    STATUS_PENDING = "pending"
    STATUS_DONE = "done"
    STATUS_OVERDUE = "overdue"
    STATUS_NA = "na"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Ожидает"),
        (STATUS_DONE, "Выполнено"),
        (STATUS_OVERDUE, "Просрочено"),
        (STATUS_NA, "Не применимо"),
        (STATUS_SKIPPED, "Пропущено"),
    ]
    CLOSED_STATUSES = (STATUS_DONE, STATUS_NA, STATUS_SKIPPED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        BankruptcyCase, on_delete=models.CASCADE,
        related_name="milestones", verbose_name="Дело",
    )
    procedure = models.ForeignKey(
        Procedure, on_delete=models.CASCADE, null=True, blank=True,
        related_name="milestones", verbose_name="Процедура",
    )
    template = models.ForeignKey(
        MilestoneTemplate, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="instances", verbose_name="Шаблон",
    )
    stage = models.ForeignKey(
        ProcedureStage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="milestones", verbose_name="Стадия",
    )
    title = models.CharField("Мероприятие", max_length=255)
    base_date_key = models.CharField("Базовая дата (якорь)", max_length=32, blank=True)
    offset_days = models.IntegerField("Смещение, дней", default=0)
    is_mandatory = models.BooleanField("Обязательное", default=True)

    due_date = models.DateField("Срок", null=True, blank=True)
    status = models.CharField(
        "Статус", max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING,
    )
    is_manual = models.BooleanField("Добавлено вручную", default=False)
    responsible = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="procedure_milestones", verbose_name="Ответственный",
    )
    done_at = models.DateTimeField("Выполнено в", null=True, blank=True)
    done_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="completed_milestones", verbose_name="Кто выполнил",
    )
    artifact_ct = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    artifact_id = models.UUIDField(null=True, blank=True)
    artifact = GenericForeignKey("artifact_ct", "artifact_id")
    notes = models.TextField("Заметки", blank=True)

    class Meta:
        verbose_name = "Мероприятие процедуры"
        verbose_name_plural = "Мероприятия процедур"
        ordering = ["procedure__order", "stage__order", "due_date", "title"]
        indexes = [
            models.Index(fields=["case", "status"]),
            models.Index(fields=["due_date"]),
            models.Index(fields=["status", "due_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["procedure", "template"],
                condition=models.Q(template__isnull=False, procedure__isnull=False),
                name="uniq_milestone_per_proc_template",
            ),
            models.UniqueConstraint(
                fields=["case", "template"],
                condition=models.Q(template__isnull=False, procedure__isnull=True),
                name="uniq_milestone_per_case_template",
            ),
        ]

    def __str__(self):
        return self.title

    @property
    def is_open(self) -> bool:
        return self.status in (self.STATUS_PENDING, self.STATUS_OVERDUE)


# ── Запросы в госорганы (раздел «Корреспонденция») ──────────────────────────

class RequestType(TimeStampedModel):
    """Каталог типов запросов (Росреестр/ГИБДД/ПФР/ФНС/ЗАГС/…) — DB-editable.

    Сроки/состав — ДАННЫЕ, правятся юристом/АУ в Справочниках (DRAFT-сид).
    AFD-шаблон документа подключается на Этапе 2 (генерация).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField("Код", max_length=60, unique=True)
    name = models.CharField("Тип запроса", max_length=255)
    default_recipient = models.ForeignKey(
        "crm.LegalEntity", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Госорган по умолчанию",
    )
    response_days = models.PositiveSmallIntegerField(
        "Срок ответа, дней", default=30,
        help_text="Через сколько дней ждём ответ (для контроля срока).",
    )
    order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активен", default=True)
    is_draft = models.BooleanField(
        "Черновик (не подтверждён)", default=True,
        help_text="Состав/сроки подлежат подтверждению юристом. Бейдж в UI.",
    )

    class Meta:
        verbose_name = "Тип запроса"
        verbose_name_plural = "Типы запросов"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class RequestPackage(TimeStampedModel):
    """Именованный пакет запросов — «Сформировать пакет» создаёт по всем типам."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField("Код", max_length=60, unique=True)
    name = models.CharField("Пакет запросов", max_length=255)
    types = models.ManyToManyField(
        RequestType, related_name="packages", blank=True, verbose_name="Типы запросов",
    )
    order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активен", default=True)
    is_draft = models.BooleanField("Черновик (не подтверждён)", default=True)

    class Meta:
        verbose_name = "Пакет запросов"
        verbose_name_plural = "Пакеты запросов"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Request(TimeStampedModel):
    """Запрос по делу (в госорган). Отправка — вручную (Этап 1), документ — Этап 2."""
    STATUS_DRAFT = "draft"
    STATUS_SENT = "sent"
    STATUS_ANSWERED = "answered"
    STATUS_NO_ANSWER = "no_answer"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_SENT, "Отправлен"),
        (STATUS_ANSWERED, "Ответ получен"),
        (STATUS_NO_ANSWER, "Без ответа"),
    ]
    METHOD_CHOICES = [
        ("email", "Email"),
        ("post", "Почта России"),
        ("courier", "Курьер"),
        ("site", "Сайт / портал"),
        ("handed", "Нарочно"),
        ("other", "Иное"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        BankruptcyCase, on_delete=models.CASCADE,
        related_name="requests", verbose_name="Дело",
    )
    request_type = models.ForeignKey(
        RequestType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Тип запроса",
    )
    title = models.CharField("Название", max_length=255)  # снапшот типа
    recipient = models.ForeignKey(
        "crm.LegalEntity", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Госорган",
    )
    recipient_name = models.CharField("Госорган (текст)", max_length=255, blank=True)

    status = models.CharField(
        "Статус", max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT,
    )
    sent_method = models.CharField(
        "Способ отправки", max_length=12, choices=METHOD_CHOICES, blank=True,
    )
    sent_date = models.DateField("Дата отправки", null=True, blank=True)
    response_days = models.PositiveSmallIntegerField(
        "Срок ответа, дней", null=True, blank=True,
    )
    due_date = models.DateField("Срок ответа (до)", null=True, blank=True)
    overdue_notified = models.BooleanField("Уведомление о просрочке отправлено", default=False)

    response_date = models.DateField("Дата ответа", null=True, blank=True)
    response_number = models.CharField("Номер ответа", max_length=120, blank=True)
    response_text = models.TextField("Текст/итог ответа", blank=True)

    created_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Кто создал",
    )
    notes = models.TextField("Заметки", blank=True)

    class Meta:
        verbose_name = "Запрос"
        verbose_name_plural = "Запросы"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["case", "status"]),
            models.Index(fields=["due_date"]),
        ]

    def __str__(self):
        return f"{self.title} → {self.recipient_display}"

    @property
    def recipient_display(self) -> str:
        if self.recipient_id and self.recipient:
            return self.recipient.short_name or self.recipient.name
        return self.recipient_name or "—"

    @property
    def is_overdue(self) -> bool:
        from django.utils import timezone
        return bool(
            self.status == self.STATUS_SENT
            and self.due_date and self.due_date < timezone.localdate()
        )
