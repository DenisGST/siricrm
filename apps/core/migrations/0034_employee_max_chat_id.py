# Generated for feat: персональные MAX-уведомления сотрудникам

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0033_employee_notify_court_events_max'),
    ]

    operations = [
        migrations.AddField(
            model_name='employee',
            name='max_chat_id',
            field=models.CharField(
                max_length=64, null=True, blank=True, unique=True,
                verbose_name='MAX chat_id (уведомления)',
                help_text='Привязывается через MAX-бота по одноразовому коду из '
                          'профиля. Нужен для персональных уведомлений в MAX '
                          '(напр. о судебных событиях).',
            ),
        ),
    ]
