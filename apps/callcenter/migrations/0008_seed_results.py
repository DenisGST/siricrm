"""Черновой справочник результатов звонка.

Формулировки правятся в Панели управления → «Колл-центр» → «Результаты
звонков»; здесь только стартовый набор, чтобы модалка не открывалась пустой.
Идемпотентно: если админ уже завёл свои результаты, ничего не добавляем.
"""
from django.db import migrations

RESULTS = [
    # (название, подсказка, цвет, порядок, предлагать следующее действие)
    ("Договорились о консультации", "Клиент записан на встречу", "success", 10, True),
    ("Думает", "Интерес есть, решение не принято", "info", 20, True),
    ("Просил перезвонить", "Сейчас неудобно говорить", "warning", 30, True),
    ("Недозвон", "Не взял трубку / сбросил", "neutral", 40, True),
    ("Не актуально", "Вопрос уже решён или не наш профиль", "neutral", 50, False),
    ("Отказ", "Не заинтересован", "error", 60, False),
    ("Ошиблись номером", "Звонок не по адресу", "neutral", 70, False),
]


def seed(apps, schema_editor):
    CallResult = apps.get_model("callcenter", "CallResult")
    if CallResult.objects.exists():
        return
    for name, hint, color, order, suggest in RESULTS:
        CallResult.objects.create(
            name=name, hint=hint, color=color, order=order,
            suggest_next_action=suggest, is_active=True,
        )


def unseed(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("callcenter", "0007_callresult_calloutcome")]
    operations = [migrations.RunPython(seed, unseed)]
