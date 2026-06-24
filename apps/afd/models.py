"""Модели АФД (Автоматическое Формирование Документов).

- ExecutorOrg     — реквизиты Исполнителя (юрлица), редактируются в UI АФД.
- DocumentTemplate — шаблон документа (.docx с плейсхолдерами вида {key}),
                     хранится в S3 через StoredFile.
- GeneratedDocument — история генераций (какой шаблон, по какой услуге, кем,
                     ссылки на .docx и .pdf в S3).
"""
import uuid

from django.db import models


class ExecutorOrg(models.Model):
    """Организация-исполнитель — источник реквизитов для подстановки в договор.

    Плейсхолдеры договора:
      {ispolnitel}             ← intro_text   (вводная строка-описание юрлица)
      {Реквизиты_исполнителя}  ← requisites   (блок ИНН/ОГРН/р-счёт/банк/адрес)
      {Исполнитель}            ← signer_name  (ФИО подписанта для строки подписи)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название (для выбора)", max_length=255)
    intro_text = models.TextField(
        "Вводная строка ({ispolnitel})",
        blank=True,
        help_text="Например: ООО «Сириус», ИНН … в лице директора …, "
                  "действующего на основании Устава",
    )
    requisites = models.TextField(
        "Реквизиты ({Реквизиты_исполнителя})",
        blank=True,
        help_text="Полный блок реквизитов: ИНН, ОГРН, р/счёт, банк, БИК, адрес и т.д.",
    )
    signer_name = models.CharField(
        "ФИО подписанта ({Исполнитель})", max_length=255, blank=True,
        help_text="Подставляется в строку подписи: ____/ФИО",
    )
    is_default = models.BooleanField(
        "По умолчанию", default=False,
        help_text="Используется, если у услуги исполнитель не выбран явно.",
    )
    is_active = models.BooleanField("Активна", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Организация-исполнитель"
        verbose_name_plural = "Организации-исполнители"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Гарантируем единственный дефолт.
        if self.is_default:
            ExecutorOrg.objects.exclude(pk=self.pk).filter(is_default=True).update(
                is_default=False
            )

    @classmethod
    def get_default(cls):
        return (
            cls.objects.filter(is_active=True, is_default=True).first()
            or cls.objects.filter(is_active=True).first()
        )


class DocumentTemplate(models.Model):
    """Шаблон документа АФД — .docx с плейсхолдерами {key}."""

    KIND_CONTRACT_BFL = "contract_bfl"
    KIND_REQUEST = "request"
    KIND_EFRSB = "efrsb"
    KIND_OTHER = "other"
    KIND_CHOICES = [
        (KIND_CONTRACT_BFL, "Договор юруслуг (БФЛ)"),
        (KIND_REQUEST, "Запрос в госорган"),
        (KIND_EFRSB, "Сообщение ЕФРСБ"),
        (KIND_OTHER, "Прочее"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=255)
    kind = models.CharField(
        "Тип документа", max_length=32, choices=KIND_CHOICES, default=KIND_OTHER,
    )
    stored_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.PROTECT,
        related_name="afd_templates", verbose_name="Файл шаблона (.docx)",
    )
    description = models.TextField("Описание / список плейсхолдеров", blank=True)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="afd_templates_updated", verbose_name="Кто изменил",
    )

    class Meta:
        verbose_name = "Шаблон документа"
        verbose_name_plural = "Шаблоны документов"
        ordering = ["kind", "name"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.name}"

    @classmethod
    def active_for_kind(cls, kind):
        return cls.objects.filter(kind=kind, is_active=True).first()


class GeneratedDocument(models.Model):
    """Запись о сгенерированном документе (история АФД)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        DocumentTemplate, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="generated", verbose_name="Шаблон",
    )
    client = models.ForeignKey(
        "crm.Client", on_delete=models.CASCADE,
        related_name="afd_documents", verbose_name="Клиент",
    )
    service = models.ForeignKey(
        "crm.Service", on_delete=models.CASCADE, null=True, blank=True,
        related_name="afd_documents", verbose_name="Услуга",
    )
    docx_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="afd_generated_docx", verbose_name="Готовый .docx",
    )
    pdf_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="afd_generated_pdf", verbose_name="Готовый .pdf",
    )
    title = models.CharField("Заголовок", max_length=255, blank=True)
    created_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="afd_documents_created", verbose_name="Кто сформировал",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Сформированный документ"
        verbose_name_plural = "Сформированные документы"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or f"Документ {self.id}"


class IskTemplate(models.Model):
    """Шаблон заявления о банкротстве — секционный (см. IskSection).

    Можно несколько вариантов (например «с имуществом» / «без имущества»).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=255)
    is_default = models.BooleanField("По умолчанию", default=False)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Шаблон заявления (иск)"
        verbose_name_plural = "Шаблоны заявлений (иски)"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            IskTemplate.objects.exclude(pk=self.pk).filter(is_default=True).update(
                is_default=False
            )

    @classmethod
    def get_default(cls):
        return (
            cls.objects.filter(is_active=True, is_default=True).first()
            or cls.objects.filter(is_active=True).first()
        )


class IskSection(models.Model):
    """Смысловой раздел заявления. Редактируется в UI (добавить/удалить/двигать).

    body — текст с плейсхолдерами вида {key} (включая вычисляемые: {debts_list},
    {creditors_block}, {sum_total}, {petition_sro}, {appendix_list} и т.д.).
    block_type — семантический ярлык (для группировки/особого рендера).
    include_condition — ключ флага в контексте; если задан и флаг ложный —
    раздел не включается (для условных разделов: имущество/семья/сделки).
    """
    BLOCK_TEXT = "text"
    BLOCK_COURT_HEADER = "court_header"
    BLOCK_CREDITORS_HEADER = "creditors_header"
    BLOCK_DEBTS_LIST = "debts_list"
    BLOCK_PETITION = "petition"
    BLOCK_APPENDIX_LIST = "appendix_list"
    BLOCK_CHOICES = [
        (BLOCK_TEXT, "Текст"),
        (BLOCK_COURT_HEADER, "Шапка (суд + должник)"),
        (BLOCK_CREDITORS_HEADER, "Блок кредиторов (реквизиты)"),
        (BLOCK_DEBTS_LIST, "Перечень задолженностей"),
        (BLOCK_PETITION, "Просительная часть"),
        (BLOCK_APPENDIX_LIST, "Перечень приложений"),
    ]

    ALIGN_CHOICES = [("left", "Слева"), ("center", "По центру"),
                     ("both", "По ширине"), ("right", "Справа")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        IskTemplate, on_delete=models.CASCADE, related_name="sections",
        verbose_name="Шаблон",
    )
    order = models.PositiveIntegerField("Порядок", default=0)
    key = models.CharField("Ключ", max_length=64, blank=True)
    title = models.CharField("Заголовок раздела", max_length=255, blank=True)
    body = models.TextField(
        "Текст (с плейсхолдерами {…})", blank=True,
        help_text="Плейсхолдеры {…} подставляются из данных. Вычисляемые: "
                  "{debts_list} {creditors_block} {sum_total} {sum_overdue} "
                  "{overdue_pct} {petition_sro} {appendix_list}",
    )
    block_type = models.CharField(
        "Тип блока", max_length=32, choices=BLOCK_CHOICES, default=BLOCK_TEXT,
    )
    align = models.CharField("Выравнивание", max_length=8, choices=ALIGN_CHOICES, default="both")
    bold = models.BooleanField("Жирный", default=False)
    is_optional = models.BooleanField("Опциональный", default=False)
    include_condition = models.CharField(
        "Условие включения (ключ флага)", max_length=64, blank=True,
        help_text="Если задан — раздел включается только когда флаг истинный "
                  "(напр. has_property, has_income, is_married, has_sold_assets).",
    )
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Раздел заявления"
        verbose_name_plural = "Разделы заявления"
        ordering = ["template", "order"]

    def __str__(self):
        return f"{self.order:02d} · {self.title or self.get_block_type_display()}"
