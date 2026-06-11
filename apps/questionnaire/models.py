import uuid
from django.db import models


QUESTION_TYPES = [
    ("text",               "Короткий текст"),
    ("textarea",           "Большой текст"),
    ("choice",             "Один вариант"),
    ("multi_choice",       "Несколько вариантов"),
    ("yes_no",             "Да / Нет / Не знаю"),
    ("number",             "Число"),
    ("money",              "Сумма (руб.)"),
    ("date",               "Дата"),
    ("full_name_date",     "ФИО + дата рождения"),
    ("region_ref",         "Регион (справочник)"),
    ("legal_entity_ref",   "Юрлицо (справочник)"),
    ("client_ref",         "Клиент (справочник)"),
    ("employee_ref",       "Сотрудник (справочник)"),
    ("repeatable_group",   "Повторяемая группа"),
    ("marital_status",     "Семейное положение"),
    ("bank_debts",         "Долги перед банками"),
    ("mfo_debts",          "Долги перед МФО"),
    ("marketplace_debts",  "Кредиты/рассрочки на маркетплейсах"),
    ("property_assets",    "Имущество в собственности"),
    ("utility_debts",      "Коммунальные платежи"),
    ("fine_debts",         "Штрафы"),
    ("court_debts",        "Задолженности по решению суда"),
    ("other_debts",        "Иные задолженности"),
    ("sold_assets",        "Проданное имущество"),
    ("tax_debts",          "Неуплаченные налоги"),
    ("children_list",      "Дети (список)"),
]


class QuestionnaireTemplate(models.Model):
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service_name = models.OneToOneField(
        "crm.ServiceName", on_delete=models.CASCADE,
        related_name="questionnaire_template", verbose_name="Услуга",
    )
    title       = models.CharField("Название анкеты", max_length=200)
    description = models.TextField("Описание", blank=True)
    is_active   = models.BooleanField("Активна", default=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Шаблон анкеты"
        verbose_name_plural = "Шаблоны анкет"
        ordering = ["service_name__short_name"]

    def __str__(self):
        return f"{self.service_name.short_name}: {self.title}"


class QuestionnairePage(models.Model):
    id       = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        QuestionnaireTemplate, on_delete=models.CASCADE,
        related_name="pages", verbose_name="Шаблон",
    )
    title = models.CharField("Заголовок страницы", max_length=200, blank=True)
    order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Страница анкеты"
        verbose_name_plural = "Страницы анкеты"
        ordering = ["order"]

    def __str__(self):
        return f"{self.template} / стр. {self.order + 1}: {self.title}"


class Question(models.Model):
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page          = models.ForeignKey(
        QuestionnairePage, on_delete=models.CASCADE,
        related_name="questions", verbose_name="Страница",
    )
    parent_group  = models.ForeignKey(
        "self", on_delete=models.CASCADE,
        null=True, blank=True, related_name="sub_questions",
        verbose_name="Родительская группа",
    )
    order         = models.PositiveIntegerField("Порядок", default=0)
    text          = models.TextField("Текст вопроса")
    hint          = models.CharField("Подсказка", max_length=500, blank=True)
    question_type = models.CharField("Тип", max_length=30, choices=QUESTION_TYPES, default="text")
    is_required   = models.BooleanField("Обязательный", default=False)
    legal_entity_kind = models.ForeignKey(
        "crm.LegalEntityKind", on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="Тип юрлица (фильтр)",
    )
    allow_custom_text = models.BooleanField("Разрешить свой текст", default=False)
    default_value     = models.CharField("Значение по умолчанию", max_length=200, blank=True)
    show_if_question  = models.ForeignKey(
        "self", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="dependent_questions",
        verbose_name="Показывать если вопрос",
    )
    show_if_value = models.CharField("имеет значение", max_length=200, blank=True)

    class Meta:
        verbose_name = "Вопрос"
        verbose_name_plural = "Вопросы"
        ordering = ["order"]

    def __str__(self):
        return f"[{self.get_question_type_display()}] {self.text[:60]}"


class QuestionChoice(models.Model):
    id       = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE,
        related_name="choices", verbose_name="Вопрос",
    )
    EXTRA_FIELD_TYPES = [
        ("text",         "Текст"),
        ("name_amount",  "Наименование + Сумма (руб.)"),
        ("client_ref",   "Выбор клиента"),
        ("employee_ref", "Выбор сотрудника"),
        ("agent_ref",    "Выбор агента"),
    ]
    text             = models.CharField("Вариант ответа", max_length=300)
    order            = models.PositiveIntegerField("Порядок", default=0)
    has_extra_field  = models.BooleanField("Доп. поле", default=False)
    extra_field_type = models.CharField("Тип доп. поля", max_length=20,
                                        choices=EXTRA_FIELD_TYPES, default="text")
    extra_field_hint = models.CharField("Подсказка доп. поля", max_length=200, blank=True)

    class Meta:
        verbose_name = "Вариант ответа"
        verbose_name_plural = "Варианты ответа"
        ordering = ["order"]

    def __str__(self):
        return self.text


class QuestionnaireResponse(models.Model):
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service      = models.ForeignKey(
        "crm.Service", on_delete=models.CASCADE,
        related_name="questionnaire_responses", verbose_name="Услуга",
    )
    template     = models.ForeignKey(
        QuestionnaireTemplate, on_delete=models.PROTECT,
        related_name="responses", verbose_name="Шаблон",
    )
    filled_by    = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="Заполнил",
    )
    current_page     = models.PositiveIntegerField("Текущая страница", default=0)
    is_complete      = models.BooleanField("Завершена", default=False)
    pdf_s3_key       = models.CharField("PDF в S3", max_length=500, blank=True)
    pdf_generated_at = models.DateTimeField("PDF сгенерирован", null=True, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ответ на анкету"
        verbose_name_plural = "Ответы на анкеты"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.template.service_name.short_name} / {self.service}"


class Answer(models.Model):
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    response    = models.ForeignKey(
        QuestionnaireResponse, on_delete=models.CASCADE,
        related_name="answers", verbose_name="Ответ на анкету",
    )
    question    = models.ForeignKey(
        Question, on_delete=models.CASCADE,
        related_name="answers", verbose_name="Вопрос",
    )
    group_index = models.PositiveIntegerField("Индекс группы", default=0)
    value       = models.JSONField("Значение", default=dict)

    class Meta:
        verbose_name = "Ответ на вопрос"
        verbose_name_plural = "Ответы на вопросы"
        unique_together = [("response", "question", "group_index")]

    def __str__(self):
        return f"{self.question.text[:40]} → {str(self.value)[:40]}"
