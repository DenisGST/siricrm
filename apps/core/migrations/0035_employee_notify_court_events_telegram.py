from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0034_employee_max_chat_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='employee',
            name='notify_court_events_telegram',
            field=models.BooleanField(
                default=False,
                verbose_name='Уведомлять в Telegram о судебных событиях',
            ),
        ),
    ]
