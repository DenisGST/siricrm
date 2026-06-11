"""
Недеструктивно добавляет вопрос «в) Кредиты/рассрочки на маркетплейсах»
(тип marketplace_debts) в раздел «Задолженности» существующих шаблонов БФЛ
и перелитеровывает последующие пункты (в→г, г→д, …).

Пересоздать шаблон командой нельзя: QuestionnaireResponse.template = PROTECT,
а заполненных анкет уже много. Поэтому правим существующие Question напрямую,
не трогая ответы.
"""
from django.db import migrations

# question_type → новая подпись (после вставки маркетплейсов)
RELETTER = {
    "tax_debts":     "г) Неуплаченные налоги",
    "utility_debts": "д) Коммунальные платежи",
    "fine_debts":    "е) Штрафы",
    "court_debts":   "ж) Задолженности по решению суда",
    "other_debts":   "з) Иные задолженности",
}
# Обратная перелитеровка (для reverse)
RELETTER_OLD = {
    "tax_debts":     "в) Неуплаченные налоги",
    "utility_debts": "г) Коммунальные платежи",
    "fine_debts":    "д) Штрафы",
    "court_debts":   "е) Задолженности по решению суда",
    "other_debts":   "ж) Иные задолженности",
}


def _debt_pages(apps):
    """Страницы «Задолженности» во всех шаблонах БФЛ."""
    QuestionnairePage = apps.get_model("questionnaire", "QuestionnairePage")
    return QuestionnairePage.objects.filter(
        template__service_name__short_name="БФЛ",
        title="Задолженности",
    )


def forwards(apps, schema_editor):
    Question = apps.get_model("questionnaire", "Question")
    for page in _debt_pages(apps):
        # Перелитеровка последующих пунктов
        for qt, text in RELETTER.items():
            page.questions.filter(question_type=qt).update(text=text)
        # Вставка нового пункта (идемпотентно)
        if not page.questions.filter(question_type="marketplace_debts").exists():
            Question.objects.create(
                page=page, order=2,
                text="в) Кредиты/рассрочки на маркетплейсах",
                question_type="marketplace_debts",
                hint="", is_required=False, allow_custom_text=False,
                legal_entity_kind=None, default_value="",
            )


def backwards(apps, schema_editor):
    for page in _debt_pages(apps):
        page.questions.filter(question_type="marketplace_debts").delete()
        for qt, text in RELETTER_OLD.items():
            page.questions.filter(question_type=qt).update(text=text)


class Migration(migrations.Migration):

    dependencies = [
        ("questionnaire", "0005_alter_question_question_type"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
