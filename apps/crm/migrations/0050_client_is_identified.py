from django.db import migrations, models


def mark_non_telegram_as_identified(apps, schema_editor):
    """
    Клиенты без telegram_id были созданы сотрудниками вручную либо иным
    способом — считаем их идентифицированными по умолчанию.
    Тем, у кого telegram_id есть, оставляем is_identified=False — их
    нужно сверить с реальным ФИО (см. модалку «Идентификация»).
    """
    Client = apps.get_model("crm", "Client")
    Client.objects.filter(telegram_id__isnull=True).update(is_identified=True)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0049_clientevent_questionnaire_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="is_identified",
            field=models.BooleanField(
                default=False,
                help_text="ФИО клиента подтверждено сотрудником через модалку «Идентификация»",
                verbose_name="Идентифицирован",
            ),
        ),
        migrations.AddIndex(
            model_name="client",
            index=models.Index(fields=["is_identified"], name="crm_client_is_iden_idx"),
        ),
        migrations.AlterField(
            model_name="clientevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("first_contact", "Первое обращение"),
                    ("status_change", "Смена статуса"),
                    ("client_identified", "Клиент идентифицирован"),
                    ("note", "Заметка"),
                    ("contract_created", "Заключение договора"),
                    ("contract_terminated", "Расторжение договора"),
                    ("employee_assigned", "Назначен сотрудник"),
                    ("employee_removed", "Сотрудник снят"),
                    ("dept_assigned", "Передан в работу отдела"),
                    ("claim_filed", "Подан иск в суд"),
                    ("hearing_scheduled", "Назначено судебное заседание"),
                    ("procedure_started", "Введена процедура"),
                    ("procedure_ended", "Окончена процедура"),
                    ("dialog_started", "Начат диалог"),
                    ("dialog_ended", "Окончен диалог"),
                    ("file_received", "Получен файл"),
                    ("file_sent", "Отправлен файл"),
                    ("letter_outgoing", "Направлено исходящее письмо"),
                    ("letter_incoming", "Получено входящее письмо"),
                    ("service_created", "Услуга добавлена"),
                    ("service_deleted", "Услуга удалена"),
                    ("consultation_booked", "Записан на консультацию"),
                    ("consultation_result", "Результат консультации"),
                    ("consultation_transferred", "Консультация перенесена"),
                    ("consultation_edited", "Консультация изменена"),
                    ("questionnaire_created", "Анкета создана"),
                    ("questionnaire_edited", "Анкета отредактирована"),
                    ("questionnaire_deleted", "Анкета удалена"),
                    ("system", "Системное событие"),
                ],
                default="note",
                max_length=30,
                verbose_name="Тип события",
            ),
        ),
        migrations.RunPython(mark_non_telegram_as_identified, noop_reverse),
    ]
