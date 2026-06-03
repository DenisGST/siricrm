"""Частичный unique-констрейнт «один инбокс на сотрудника».

Отдельной миграцией (после 0076 с RunPython-сидом) — иначе CREATE INDEX
падает с «pending trigger events» в одной транзакции с DML.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0076_service_employee_status_inbox'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='serviceemployeestatus',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_inbox', True)),
                fields=('employee',),
                name='unique_inbox_status_per_employee',
            ),
        ),
    ]
