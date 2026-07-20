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
    FILING_METHOD_CHOICES = [
        ("post", "Почта России"),
        ("court_office", "Канцелярия суда"),
        ("kad", "Сайт суда (kad.arbitr.ru)"),
    ]
    filing_method = models.CharField(
        "Способ отправки иска", max_length=16,
        choices=FILING_METHOD_CHOICES, blank=True,
    )
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
    # ФУ назначается судом при введении. Реквизиты — из справочника
    # «Арбитражные управляющие» (arbitr_manager); поля ниже — legacy/fallback.
    arbitr_manager = models.ForeignKey(
        "ArbitrationManager", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="procedures", verbose_name="Финуправляющий (АУ)",
    )
    financial_manager = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="managed_procedures", verbose_name="Финуправляющий (штатный, legacy)",
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
        if self.arbitr_manager_id and self.arbitr_manager:
            return self.arbitr_manager.full_fio
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
    # Способ определения адресата по типу запроса + региону/адресу клиента.
    LOOKUP_NONE = "none"            # адресат не нужен (Росреестр — через СМЭВ)
    LOOKUP_REGION = "region"        # подбор по виду ЮЛ + региону (+ район гибридом)
    LOOKUP_FNS = "fns_by_address"   # ФНС по коду ИФНС из адреса клиента
    LOOKUP_MANUAL = "manual"        # только ручной выбор (Банк и пр.)
    # Адресат — не госорган: сам должник или его кредиторы (уведомления ФУ).
    LOOKUP_DEBTOR = "debtor"        # адресат = должник (клиент) — ФИО+адрес из карточки
    LOOKUP_CREDITORS = "creditors"  # адресат = кредиторы: по письму НА КАЖДОГО из анкеты
    LOOKUP_CHOICES = [
        (LOOKUP_NONE, "Не требуется (СМЭВ)"),
        (LOOKUP_REGION, "По виду и региону клиента"),
        (LOOKUP_FNS, "ФНС по адресу клиента"),
        (LOOKUP_MANUAL, "Только вручную"),
        (LOOKUP_DEBTOR, "Должник (сам клиент)"),
        (LOOKUP_CREDITORS, "Кредиторы (письмо каждому)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField("Код", max_length=60, unique=True)
    name = models.CharField("Тип запроса", max_length=255)
    default_recipient = models.ForeignKey(
        "crm.LegalEntity", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Госорган по умолчанию",
    )
    recipient_kind = models.ForeignKey(
        "crm.LegalEntityKind", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Вид госоргана (для подбора)",
        help_text="Какой вид ЮЛ подбирать в адресаты (МРЭО/ГИМС/ФНС/ЗАГС/суд…).",
    )
    recipient_lookup = models.CharField(
        "Способ определения адресата", max_length=16,
        choices=LOOKUP_CHOICES, default=LOOKUP_MANUAL,
    )
    response_days = models.PositiveSmallIntegerField(
        "Срок ответа, дней", default=30,
        help_text="Через сколько дней ждём ответ (для контроля срока).",
    )
    template = models.ForeignKey(
        "afd.DocumentTemplate", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Шаблон документа (.docx)",
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


class RecipientRule(TimeStampedModel):
    """Запомненный выбор госоргана для (вид ЮЛ + регион [+ район/город]).

    Когда юрист вручную выбирает адресата, можно сохранить выбор — и он
    переиспользуется при формировании запросов у ДРУГИХ клиентов того же
    региона/района. Так справочник районного уровня (МРЭО/ЗАГС/суд) постепенно
    «обучается» без ручного поиска каждый раз.

    `district` — нормализованное (lower) название района/города; пусто →
    правило на весь регион. Резолвер сначала ищет точное (регион+район),
    затем правило на весь регион.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.ForeignKey(
        "crm.LegalEntityKind", on_delete=models.CASCADE,
        related_name="+", verbose_name="Вид госоргана",
    )
    region = models.ForeignKey(
        "crm.Region", on_delete=models.CASCADE,
        related_name="+", verbose_name="Регион",
    )
    district = models.CharField(
        "Район/город (нормализованный)", max_length=255, blank=True, default="",
    )
    recipient = models.ForeignKey(
        "crm.LegalEntity", on_delete=models.CASCADE,
        related_name="+", verbose_name="Госорган",
    )
    created_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Кто сохранил",
    )

    class Meta:
        verbose_name = "Правило адресата"
        verbose_name_plural = "Правила адресатов"
        ordering = ["kind__short_name", "region__number", "district"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "region", "district"],
                name="uniq_recipient_rule_kind_region_district",
            ),
        ]

    def __str__(self):
        loc = f"/{self.district}" if self.district else ""
        return f"{self.kind} · {self.region.number}{loc} → {self.recipient}"


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
    bubble_id = models.CharField(
        "Bubble ID", max_length=64, blank=True, null=True, unique=True, db_index=True,
        help_text="ID записи Сorrespondence в исходной CRM на bubble.io (для идемпотентного импорта).",
    )
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
    response_scan = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Скан ответа",
    )

    # Сформированный документ запроса (исходящее письмо).
    outgoing_number = models.PositiveIntegerField("Исходящий №", null=True, blank=True)
    with_signature = models.BooleanField("С подписью и печатью", default=False)
    generated_at = models.DateTimeField("Сформирован", null=True, blank=True)
    document_pdf = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Документ (PDF)",
    )
    document_docx = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Документ (.docx)",
    )
    pages_count = models.PositiveSmallIntegerField(
        "Страниц в документе", null=True, blank=True,
        help_text="Считается при формировании/загрузке PDF. Нужен для веса письма "
                  "в выгрузке для Почты России (пустой → вес по умолчанию).",
    )

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


# ── Арбитражные управляющие (справочник реквизитов ФУ) ──────────────────────

class ArbitrationManager(TimeStampedModel):
    """Справочник АУ: реквизиты финуправляющего для документов-запросов.

    ИНН/СНИЛС/адрес/тел/email/СРО — подставляются в шаблоны. PNG подписи и
    печати (опц.) накладываются при формировании «с подписью и печатью».
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    last_name = models.CharField("Фамилия", max_length=120)
    first_name = models.CharField("Имя", max_length=120)
    patronymic = models.CharField("Отчество", max_length=120, blank=True)
    inn = models.CharField("ИНН", max_length=20, blank=True)
    snils = models.CharField("СНИЛС", max_length=20, blank=True)
    corr_address = models.CharField("Адрес для корреспонденции", max_length=400, blank=True)
    ops_index = models.CharField(
        "Индекс ОПС отправки", max_length=6, blank=True,
        help_text="Шестизначный индекс отделения, через которое АУ сдаёт почту "
                  "(колонка INDEXFROM в выгрузке для Почты России).",
    )
    phone = models.CharField("Телефон / факс", max_length=120, blank=True)
    email = models.CharField("E-mail", max_length=255, blank=True)
    sro = models.ForeignKey(
        "crm.LegalEntity", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="СРО",
    )
    sro_text = models.CharField(
        "Реквизиты СРО (текст)", max_length=500, blank=True,
        help_text="Если СРО не выбран из реестра — текстом для подстановки.",
    )
    employee = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="arbitration_profiles", verbose_name="Сотрудник (если штатный)",
    )
    signature_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Подпись и печать (PNG)",
    )
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Арбитражный управляющий"
        verbose_name_plural = "Арбитражные управляющие"
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return self.short_fio

    @property
    def full_fio(self) -> str:
        return " ".join(filter(None, [self.last_name, self.first_name, self.patronymic])).strip()

    @property
    def short_fio(self) -> str:
        ini = ""
        if self.first_name:
            ini += self.first_name[0] + "."
        if self.patronymic:
            ini += self.patronymic[0] + "."
        return (f"{self.last_name} {ini}".strip()) or "—"

    @property
    def sro_display(self) -> str:
        if self.sro_id and self.sro:
            return self.sro.name
        return self.sro_text or ""


# ── Активы должника (вкладка «Активы») ──────────────────────────────────────
# Наполняются парсером пакета ответов ФНС (fns_parser.py) и правятся вручную.
# Субъект сведений — должник ИЛИ его супруг: ФНС отвечает и по супругу тоже
# (в справке «Тип субъекта запроса: Супруг(супруга) должника ФЛ»).

SUBJECT_DEBTOR = "debtor"
SUBJECT_SPOUSE = "spouse"
SUBJECT_CHOICES = [
    (SUBJECT_DEBTOR, "Должник"),
    (SUBJECT_SPOUSE, "Супруг(а)"),
]


class AssetDocument(TimeStampedModel):
    """Загруженный документ-источник сведений об активах (пакет ответа ФНС).

    Хранит исходный файл (S3) + сырой результат парсинга (`raw`) — чтобы можно
    было переразобрать/сверить, не запрашивая ФНС повторно. Удаление документа
    каскадом сносит все распознанные из него записи.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        "BankruptcyCase", on_delete=models.CASCADE,
        related_name="asset_documents", verbose_name="Дело",
    )
    stored_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Файл",
    )
    filename = models.CharField("Имя файла", max_length=400, blank=True)

    subject = models.CharField(
        "Субъект сведений", max_length=10, choices=SUBJECT_CHOICES, default=SUBJECT_DEBTOR,
    )
    subject_fio = models.CharField("ФИО субъекта", max_length=255, blank=True)
    subject_inn = models.CharField("ИНН субъекта", max_length=12, blank=True)
    subject_birth_date = models.DateField("Дата рождения субъекта", null=True, blank=True)

    debtor_fio = models.CharField("ФИО должника (по справке)", max_length=255, blank=True)
    debtor_inn = models.CharField("ИНН должника (по справке)", max_length=12, blank=True)
    court_name = models.CharField("Суд (по справке)", max_length=255, blank=True)
    case_number = models.CharField("№ дела (по справке)", max_length=64, blank=True)

    formed_at = models.DateField("Дата формирования сведений", null=True, blank=True)
    tax_authority = models.CharField("Налоговый орган", max_length=400, blank=True)
    has_tax_debt = models.BooleanField("Есть налоговая задолженность", null=True, blank=True)

    raw = models.JSONField("Результат парсинга (сырой)", default=dict, blank=True)
    uploaded_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Загрузил",
    )

    class Meta:
        verbose_name = "Документ-источник (активы)"
        verbose_name_plural = "Документы-источники (активы)"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.filename or 'Ответ ФНС'} · {self.subject_fio}"


class AssetRecordBase(TimeStampedModel):
    """Общее для всех распознанных записей: дело, документ-источник, субъект."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        "BankruptcyCase", on_delete=models.CASCADE,
        related_name="%(class)ss", verbose_name="Дело",
    )
    document = models.ForeignKey(
        "AssetDocument", on_delete=models.CASCADE, null=True, blank=True,
        related_name="%(class)ss", verbose_name="Источник",
    )
    subject = models.CharField(
        "Субъект", max_length=10, choices=SUBJECT_CHOICES, default=SUBJECT_DEBTOR,
    )
    notes = models.TextField("Заметки", blank=True)

    class Meta:
        abstract = True


class BankAccount(AssetRecordBase):
    """Счёт / вклад / ЭСП из справки ФНС (ст. 86 НК).

    Реквизиты банка идут прямо в справке (ИНН/КПП/БИК/адрес). `legal_entity` —
    матч по ИНН в реестре `crm.LegalEntity` (адресат будущего запроса о выписке).
    🛑 Один банк = несколько блоков (Сбербанк: головной + отделения; разные КПП
    и БИК, один ИНН) — группировать для запроса надо по ИНН, не по блоку.
    """
    STATE_OPEN = "open"
    STATE_CLOSED = "closed"
    STATE_REVOKED = "revoked"          # прекращено право использования (ЭСП)
    STATE_GRANTED = "granted"          # предоставлено право использования (ЭСП)
    STATE_LIQUIDATED_BANK = "liq_bank" # в ликвидированном банке
    STATE_CHOICES = [
        (STATE_OPEN, "Открыт"),
        (STATE_CLOSED, "Закрыт"),
        (STATE_REVOKED, "Прекращено право использования"),
        (STATE_GRANTED, "Предоставлено право использования"),
        (STATE_LIQUIDATED_BANK, "В ликвидированном банке"),
    ]

    number = models.CharField("Номер счёта / ЭСП", max_length=32)
    opened_date = models.DateField("Дата открытия", null=True, blank=True)
    closed_date = models.DateField("Дата закрытия", null=True, blank=True)
    state = models.CharField("Состояние", max_length=16, choices=STATE_CHOICES, blank=True)
    state_text = models.CharField("Состояние (как в справке)", max_length=120, blank=True)
    account_kind = models.CharField("Вид счёта", max_length=120, blank=True)

    bank_name = models.CharField("Банк", max_length=400)
    bank_inn = models.CharField("ИНН банка", max_length=12, blank=True)
    bank_kpp = models.CharField("КПП банка", max_length=9, blank=True)
    bank_bik = models.CharField("БИК банка", max_length=12, blank=True)
    bank_regnum = models.CharField("РегНом/НомФ", max_length=32, blank=True)
    bank_address = models.CharField("Адрес банка", max_length=500, blank=True)
    legal_entity = models.ForeignKey(
        "crm.LegalEntity", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Банк в реестре",
    )
    statement_request = models.ForeignKey(
        "Request", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bank_accounts", verbose_name="Запрос выписки",
    )

    class Meta:
        verbose_name = "Счёт в банке"
        verbose_name_plural = "Счета в банках"
        ordering = ["bank_name", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "subject", "number"], name="uniq_account_per_case_subject",
            ),
        ]

    def __str__(self):
        return f"{self.number} · {self.bank_name}"

    @property
    def is_open(self) -> bool:
        return self.state in (self.STATE_OPEN, self.STATE_GRANTED)


class IncomeCertificate(AssetRecordBase):
    """Справка о доходах 2-НДФЛ (КНД 1175018) — одна на пару «год + агент»."""
    year = models.PositiveIntegerField("Год")
    cert_date = models.DateField("Дата справки", null=True, blank=True)
    agent_name = models.CharField("Налоговый агент", max_length=500)
    agent_inn = models.CharField("ИНН агента", max_length=12, blank=True)
    agent_kpp = models.CharField("КПП агента", max_length=9, blank=True)
    oktmo = models.CharField("ОКТМО", max_length=11, blank=True)
    total_income = models.DecimalField(
        "Общая сумма дохода", max_digits=14, decimal_places=2, null=True, blank=True,
    )
    tax_base = models.DecimalField(
        "Налоговая база", max_digits=14, decimal_places=2, null=True, blank=True,
    )
    tax_calculated = models.DecimalField(
        "Налог исчислен", max_digits=14, decimal_places=2, null=True, blank=True,
    )
    tax_withheld = models.DecimalField(
        "Налог удержан", max_digits=14, decimal_places=2, null=True, blank=True,
    )
    rows = models.JSONField("Доходы по месяцам", default=list, blank=True)

    class Meta:
        verbose_name = "Справка 2-НДФЛ"
        verbose_name_plural = "Справки 2-НДФЛ"
        ordering = ["-year", "agent_name"]

    def __str__(self):
        return f"2-НДФЛ {self.year} · {self.agent_name}"


class RealEstateObject(AssetRecordBase):
    """Объект недвижимости (раздел 1 сведений об объектах налогообложения)."""
    object_type = models.CharField("Вид объекта", max_length=200, blank=True)
    address = models.CharField("Адрес", max_length=600, blank=True)
    area = models.CharField("Площадь (м²)", max_length=32, blank=True)
    share = models.CharField("Доля в праве", max_length=32, blank=True)
    cadastral_number = models.CharField("Кадастровый номер", max_length=64, blank=True)
    cadastral_value = models.DecimalField(
        "Кадастровая стоимость", max_digits=16, decimal_places=2, null=True, blank=True,
    )
    commissioned_date = models.DateField("Дата ввода в эксплуатацию", null=True, blank=True)
    reg_date = models.DateField("Дата регистрации владения", null=True, blank=True)
    dereg_date = models.DateField("Дата прекращения владения", null=True, blank=True)

    class Meta:
        verbose_name = "Объект недвижимости"
        verbose_name_plural = "Объекты недвижимости"
        ordering = ["object_type", "address"]

    def __str__(self):
        return f"{self.object_type} · {self.cadastral_number or self.address}"


class LandPlot(AssetRecordBase):
    """Земельный участок (раздел 2 сведений об объектах налогообложения)."""
    category = models.CharField("Категория земли", max_length=200, blank=True)
    address = models.CharField("Адрес", max_length=600, blank=True)
    area = models.CharField("Площадь (м²)", max_length=32, blank=True)
    share = models.CharField("Доля в праве", max_length=32, blank=True)
    cadastral_number = models.CharField("Кадастровый номер", max_length=64, blank=True)
    cadastral_value = models.DecimalField(
        "Кадастровая стоимость", max_digits=16, decimal_places=2, null=True, blank=True,
    )
    reg_date = models.DateField("Дата регистрации владения", null=True, blank=True)
    dereg_date = models.DateField("Дата прекращения владения", null=True, blank=True)

    class Meta:
        verbose_name = "Земельный участок"
        verbose_name_plural = "Земельные участки"
        ordering = ["address"]

    def __str__(self):
        return f"{self.cadastral_number or self.address}"


class Vehicle(AssetRecordBase):
    """Транспортное средство (раздел 3 сведений об объектах налогообложения)."""
    ownership_kind = models.CharField("Вид собственности", max_length=200, blank=True)
    year = models.CharField("Год выпуска", max_length=8, blank=True)
    model = models.CharField("Марка (модель)", max_length=200, blank=True)
    power = models.CharField("Мощность (л/с)", max_length=32, blank=True)
    reg_authority = models.CharField("Регистрирующий орган", max_length=120, blank=True)
    plate = models.CharField("Гос. рег. знак", max_length=32, blank=True)
    vin = models.CharField("VIN / рег. номер", max_length=64, blank=True)
    pts = models.CharField("ПТС", max_length=64, blank=True)
    reg_date = models.DateField("Дата регистрации владения", null=True, blank=True)
    dereg_date = models.DateField("Дата прекращения владения", null=True, blank=True)

    class Meta:
        verbose_name = "Транспортное средство"
        verbose_name_plural = "Транспортные средства"
        ordering = ["model"]

    def __str__(self):
        return f"{self.model} {self.year} · {self.plate}"

    @property
    def is_owned(self) -> bool:
        """Владение не прекращено (в справке пусто в «Дата прекращения»)."""
        return self.dereg_date is None


class OtherAsset(AssetRecordBase):
    """Прочие сведения из справки: участие в ЮЛ, налоговая задолженность,
    административные правонарушения + ручные записи «иное»."""
    KIND_LEGAL_ENTITY = "legal_entity"
    KIND_TAX_DEBT = "tax_debt"
    KIND_ADMIN_OFFENSE = "admin_offense"
    KIND_OTHER = "other"
    KIND_CHOICES = [
        (KIND_LEGAL_ENTITY, "Участие в юридических лицах"),
        (KIND_TAX_DEBT, "Задолженность по налогам"),
        (KIND_ADMIN_OFFENSE, "Административное правонарушение"),
        (KIND_OTHER, "Иное"),
    ]

    kind = models.CharField("Вид", max_length=20, choices=KIND_CHOICES, default=KIND_OTHER)
    title = models.CharField("Наименование", max_length=400)
    details = models.TextField("Подробности", blank=True)
    amount = models.DecimalField(
        "Сумма", max_digits=16, decimal_places=2, null=True, blank=True,
    )
    date = models.DateField("Дата", null=True, blank=True)

    class Meta:
        verbose_name = "Иной актив / сведение"
        verbose_name_plural = "Иные активы / сведения"
        ordering = ["kind", "title"]

    def __str__(self):
        return self.title
