"""Флаг is_manual на типах событий/действий + ActionType «Комментарий сотрудника».

Различаем типы, которые ставятся автоматически (мессенджер, смена статуса,
импорт, платежи и т.п.), и те, что сотрудник добавляет вручную (комментарий,
заметка, звонок, входящее письмо, события суда). Форма ручного добавления в
логе клиента показывает только is_manual=True. is_system при этом остаётся
исключительно про защиту от удаления — на ручной выбор больше не влияет.
"""
from django.db import migrations, models


# Дефолтная разметка «ручных» типов (потом правится галкой в справочниках).
MANUAL_ACTION_CODES = {
    "employee_comment",      # новый, см. ниже
    "note",                  # Заметка
    "note_to_colleague",     # Сообщение коллеге
    "call_client",           # Звонок клиенту (пока вручную, позже — телефония)
    "letter_outgoing",       # Отправка исходящего письма
    "contract_created",      # Заключение договора
    "contract_terminated",   # Расторжение договора
    "claim_filed",           # Подача иска в суд
}
MANUAL_EVENT_CODES = {
    "letter_incoming",       # Получено входящее письмо (бумажное — вручную)
    "procedure_started",     # Введена процедура
    "hearing_scheduled",     # Назначено судебное заседание
    "procedure_ended",       # Окончена процедура
}


def seed(apps, schema_editor):
    EventType = apps.get_model("crm", "EventType")
    ActionType = apps.get_model("crm", "ActionType")

    # Новый тип действия — дефолт в форме ручного добавления.
    ActionType.objects.update_or_create(
        code="employee_comment",
        defaults={
            "name": "Комментарий сотрудника",
            "description": "Свободный комментарий сотрудника по клиенту.",
            "is_system": True,    # защищаем от удаления
            "is_manual": True,
            "is_active": True,
            "order": 0,           # первым в списке
        },
    )

    ActionType.objects.filter(code__in=MANUAL_ACTION_CODES).update(is_manual=True)
    EventType.objects.filter(code__in=MANUAL_EVENT_CODES).update(is_manual=True)


def unseed(apps, schema_editor):
    ActionType = apps.get_model("crm", "ActionType")
    ActionType.objects.filter(code="employee_comment").delete()
    # is_manual снимется вместе с RemoveField — отдельно не трогаем.


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0074_backfill_log_stored_file'),
    ]

    operations = [
        migrations.AddField(
            model_name='eventtype',
            name='is_manual',
            field=models.BooleanField(default=False, help_text='Тип можно выбрать в форме ручного добавления записи в логе клиента. Авто-генерируемые типы (мессенджер, смена статуса, импорт и т.п.) не помечаются.', verbose_name='Доступно для ручного добавления'),
        ),
        migrations.AddField(
            model_name='actiontype',
            name='is_manual',
            field=models.BooleanField(default=False, help_text='Тип можно выбрать в форме ручного добавления записи в логе клиента. Авто-генерируемые типы (отправка файла из чата, создание услуги, платежи и т.п.) не помечаются.', verbose_name='Доступно для ручного добавления'),
        ),
        migrations.RunPython(seed, unseed),
    ]
