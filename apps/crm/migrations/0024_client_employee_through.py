from django.db import migrations, models
import django.db.models.deletion


def copy_m2m_data(apps, schema_editor):
    """Copy existing M2M data from auto table to ClientEmployee."""
    ClientEmployee = apps.get_model('crm', 'ClientEmployee')
    db_alias = schema_editor.connection.alias
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT client_id, employee_id FROM crm_client_employees"
        )
        rows = cursor.fetchall()
    objs = [
        ClientEmployee(client_id=cid, employee_id=eid, messenger_status='closed')
        for cid, eid in rows
    ]
    ClientEmployee.objects.using(db_alias).bulk_create(objs, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_seed_menu_widgets'),
        ('crm', '0023_legal_entity_model'),
    ]

    operations = [
        # 1. Create the through table
        migrations.CreateModel(
            name='ClientEmployee',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('messenger_status', models.CharField(choices=[('open', 'Диалог открыт'), ('waiting', 'Ожидаю ответа'), ('closed', 'Диалог закрыт')], default='closed', max_length=10, verbose_name='Статус мессенджера')),
                ('status_changed_at', models.DateTimeField(blank=True, null=True, verbose_name='Время изменения статуса')),
                ('client', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='client_employees', to='crm.client')),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='client_employees', to='core.employee')),
            ],
            options={
                'verbose_name': 'Связь клиент-сотрудник',
                'verbose_name_plural': 'Связи клиент-сотрудник',
                'unique_together': {('client', 'employee')},
            },
        ),
        # 2. Copy existing M2M data
        migrations.RunPython(copy_m2m_data, migrations.RunPython.noop),
        # 3. Remove old auto M2M field
        migrations.RemoveField(
            model_name='client',
            name='employees',
        ),
        # 4. Add new M2M field with through=
        migrations.AddField(
            model_name='client',
            name='employees',
            field=models.ManyToManyField(
                blank=True,
                help_text='Сотрудники, работающие с клиентом',
                related_name='clients',
                through='crm.ClientEmployee',
                to='core.employee',
            ),
        ),
    ]
