from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0039_service_contract_price"),
    ]

    operations = [
        # Таблица пустая, поэтому просто удаляем старый FK и добавляем новый.
        migrations.RemoveConstraint(
            model_name="serviceemployeestatus",
            name="unique_emp_status_per_emp_service",
        ),
        migrations.RemoveField(
            model_name="serviceemployeestatus",
            name="service_name",
        ),
        migrations.AddField(
            model_name="serviceemployeestatus",
            name="common_status",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="employee_statuses",
                to="crm.servicecommonstatus",
                verbose_name="Общий статус услуги",
            ),
            # Таблица пустая — Django не спросит про дефолт.
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name="serviceemployeestatus",
            constraint=models.UniqueConstraint(
                fields=["employee", "common_status", "name"],
                name="unique_emp_status_per_emp_common_status",
            ),
        ),
        migrations.AlterModelOptions(
            name="serviceemployeestatus",
            options={
                "ordering": [
                    "employee",
                    "common_status__service_name",
                    "common_status__order",
                    "order",
                    "name",
                ],
                "verbose_name": "Статус услуги сотрудника",
                "verbose_name_plural": "Статусы услуг сотрудников",
            },
        ),
    ]
