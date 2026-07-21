"""Модели публикаций в газете «Коммерсантъ» (bankruptcy.kommersant.ru).

Слой над разделом «Процедуры банкротства» (apps.procedure), брат apps.efrsb.
Назначение:
  • KommersantMessageType — каталог типов сообщений (DRAFT, правится в Справочниках):
                            шаблон текста + какой чекбокс отмечать в бланке заявки.
  • KommersantPublication — заявка на публикацию и её жизненный цикл:
                            текст → бланк → отправка в ИД → счёт → оплата → выход.
  • KommersantAttachment  — подтверждающие документы, уходящие письмом вместе с заявкой
                            (судебный акт о введении процедуры, акт о полномочиях АУ).

🛑 Ключевое отличие от ЕФРСБ — ДЕНЕЖНЫЙ КОНТУР. Публикация платная и предоплатная:
   ИД выставляет счёт письмом, деньги должны дойти до 14:00 мск в дату окончания приёма,
   иначе сообщение уедет в следующий номер. Поэтому статусы `sent`/`invoiced`/`paid`
   существуют отдельно, а не схлопнуты в «отправлено».

🛑 Почтовые креды АУ — на `procedure.ArbitrationManager` (пароль под Fernet), не здесь.
"""
from __future__ import annotations

import uuid

from django.db import models

from apps.core.models import TimeStampedModel

# Виды процедур (для applicable_kinds) — зеркалят apps.procedure.
KIND_RESTRUCTURING = "restructuring"
KIND_REALIZATION = "realization"

# Официальный адрес приёма заявок ИД «Коммерсантъ» (bankruptcy.kommersant.ru/index.php?publemail).
KOMMERSANT_EMAIL = "pb@kommersant.ru"


class KommersantMessageType(TimeStampedModel):
    """Тип сообщения для публикации в «Коммерсантъ» (DB-editable, DRAFT).

    Бланк заявки ИД содержит ровно два взаимоисключающих чекбокса для физлиц —
    реструктуризация долгов и реализация имущества. `blank_checkbox` говорит,
    какой из них отметить при формировании заявки.
    """
    CHECKBOX_RESTRUCTURING = "restructuring"
    CHECKBOX_REALIZATION = "realization"
    CHECKBOX_CHOICES = [
        (CHECKBOX_RESTRUCTURING, "О введении реструктуризации долгов"),
        (CHECKBOX_REALIZATION, "О введении реализации имущества"),
        ("", "Не отмечать"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField("Код", max_length=64, unique=True)
    name = models.CharField("Наименование", max_length=255)
    description = models.TextField("Описание", blank=True)
    applicable_kinds = models.JSONField(
        "Виды процедур", default=list, blank=True,
        help_text="Список из restructuring/realization. Пусто — показывать всегда.",
    )
    blank_checkbox = models.CharField(
        "Чекбокс в бланке заявки", max_length=32, choices=CHECKBOX_CHOICES, blank=True,
    )
    text_template = models.TextField(
        "Шаблон текста сообщения", blank=True,
        help_text="Плейсхолдеры вида {ФИО должника} — см. подсказку в справочнике.",
    )
    order = models.PositiveIntegerField("Порядок", default=100)
    is_active = models.BooleanField("Активен", default=True)
    is_draft = models.BooleanField(
        "Черновик (не согласован с АУ)", default=True,
        help_text="Снимите, когда формулировку подтвердил арбитражный управляющий.",
    )

    class Meta:
        verbose_name = "Тип сообщения (Коммерсантъ)"
        verbose_name_plural = "Типы сообщений (Коммерсантъ)"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    def applies_to_kind(self, kind: str) -> bool:
        """Подходит ли тип виду процедуры (пустой список — подходит всем)."""
        if not self.applicable_kinds:
            return True
        return kind in self.applicable_kinds


class KommersantPublication(TimeStampedModel):
    """Заявка на публикацию сообщения о банкротстве и её жизненный цикл.

    Поля-даты денежного контура сознательно раздельные: `sent_at` (ушла заявка),
    `invoice_received_at` (пришёл счёт), `paid_date` (оплатили), `publication_date`
    (вышло в номере) — по ним юрист видит, где именно застряла публикация.
    """
    STATUS_DRAFT = "draft"
    STATUS_GENERATED = "generated"
    STATUS_SENT = "sent"
    STATUS_INVOICED = "invoiced"
    STATUS_PAID = "paid"
    STATUS_PUBLISHED = "published"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_GENERATED, "Текст сформирован"),
        (STATUS_SENT, "Заявка отправлена"),
        (STATUS_INVOICED, "Счёт получен"),
        (STATUS_PAID, "Оплачено"),
        (STATUS_PUBLISHED, "Опубликовано"),
        (STATUS_CANCELLED, "Отменено"),
    ]

    DOCS_TO_MANAGER = "manager"
    DOCS_TO_DEBTOR = "debtor"
    DOCS_TO_CHOICES = [
        (DOCS_TO_MANAGER, "На арбитражного управляющего"),
        (DOCS_TO_DEBTOR, "На должника"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        "procedure.BankruptcyCase", on_delete=models.CASCADE,
        related_name="kommersant_publications", verbose_name="Дело",
    )
    procedure = models.ForeignKey(
        "procedure.Procedure", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="kommersant_publications", verbose_name="Процедура",
    )
    message_type = models.ForeignKey(
        KommersantMessageType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="publications", verbose_name="Тип сообщения",
    )
    status = models.CharField(
        "Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True,
    )

    # ── Наш контент ──
    title = models.CharField("Заголовок", max_length=255, blank=True)
    text = models.TextField("Текст сообщения", blank=True)
    overrides = models.JSONField(
        "Ручные правки полей", default=dict, blank=True,
        help_text="Поля, введённые вручную поверх данных CRM.",
    )
    accounting_docs_to = models.CharField(
        "Отчётные документы оформить на", max_length=16,
        choices=DOCS_TO_CHOICES, default=DOCS_TO_MANAGER,
    )
    generated_at = models.DateTimeField("Текст сформирован", null=True, blank=True)

    # ── Бланк заявки (то, что уходит в ИД) ──
    blank_docx = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Заявка .docx",
    )
    blank_pdf = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Заявка .pdf (с подписью)",
    )

    # ── Отправка письма ──
    sent_at = models.DateTimeField("Заявка отправлена", null=True, blank=True)
    sent_to = models.CharField("Куда отправлено", max_length=255, blank=True)
    sent_from = models.CharField("С какого адреса", max_length=255, blank=True)
    sent_message_id = models.CharField(
        "Message-ID письма", max_length=255, blank=True, db_index=True,
        help_text="По нему ловим ответ ИД со счётом (In-Reply-To/References).",
    )
    sent_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Кто отправил",
    )
    send_error = models.TextField("Ошибка отправки", blank=True)

    # ── Счёт от ИД ──
    invoice_received_at = models.DateTimeField("Счёт получен", null=True, blank=True)
    invoice_number = models.CharField("Номер счёта", max_length=64, blank=True)
    invoice_date = models.DateField("Дата счёта", null=True, blank=True)
    invoice_amount = models.DecimalField(
        "Сумма счёта, ₽", max_digits=12, decimal_places=2, null=True, blank=True,
    )
    invoice_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Файл счёта",
    )
    invoice_message_id = models.CharField(
        "Message-ID письма со счётом", max_length=255, blank=True, db_index=True,
    )

    # ── Оплата ──
    is_paid = models.BooleanField("Счёт оплачен", default=False)
    paid_date = models.DateField("Дата оплаты", null=True, blank=True)

    # ── Факт публикации ──
    publication_date = models.DateField("Дата публикации", null=True, blank=True)
    newspaper_number = models.CharField("Номер газеты", max_length=64, blank=True)
    announcement_number = models.CharField("Номер объявления", max_length=64, blank=True)

    created_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Кто создал",
    )
    notes = models.TextField("Примечания", blank=True)

    class Meta:
        verbose_name = "Публикация в «Коммерсантъ»"
        verbose_name_plural = "Публикации в «Коммерсантъ»"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["case", "status"])]

    def __str__(self):
        return self.title or (self.message_type.name if self.message_type_id else "Публикация")

    @property
    def type_label(self) -> str:
        return self.message_type.name if self.message_type_id else "Сообщение о банкротстве"

    @property
    def is_awaiting_invoice(self) -> bool:
        """Заявка ушла, счёт ещё не пришёл — такие публикации опрашивает IMAP-поллер."""
        return self.status == self.STATUS_SENT and self.invoice_received_at is None


class KommersantAttachment(TimeStampedModel):
    """Подтверждающий документ, уходящий в ИД вместе с заявкой.

    ИД отказывает в публикации при непредставлении подтверждающих документов, поэтому
    состав вложений — часть заявки, а не «файлы где-то в деле».
    """
    KIND_COURT_ACT = "court_act"
    KIND_AUTHORITY = "authority"
    KIND_OTHER = "other"
    KIND_CHOICES = [
        (KIND_COURT_ACT, "Судебный акт о введении процедуры"),
        (KIND_AUTHORITY, "Акт о полномочиях управляющего"),
        (KIND_OTHER, "Иной документ"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(
        KommersantPublication, on_delete=models.CASCADE,
        related_name="attachments", verbose_name="Публикация",
    )
    stored_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.CASCADE,
        related_name="+", verbose_name="Файл",
    )
    kind = models.CharField(
        "Вид документа", max_length=32, choices=KIND_CHOICES, default=KIND_OTHER,
    )
    order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Вложение к заявке"
        verbose_name_plural = "Вложения к заявке"
        ordering = ["order", "created_at"]

    def __str__(self):
        return self.stored_file.filename if self.stored_file_id else "Вложение"
