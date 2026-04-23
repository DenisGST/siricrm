from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0035_seed_kind_dmi"),
    ]

    operations = [
        migrations.AlterField(
            model_name="address",
            name="client",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="addresses",
                to="crm.client",
                verbose_name="Клиент",
            ),
        ),
        # court_address: TextField → FK(Address). Старое текстовое поле
        # удаляем (в регионах оно пустое), добавляем FK с тем же именем.
        migrations.RemoveField(
            model_name="region",
            name="court_address",
        ),
        migrations.AddField(
            model_name="region",
            name="court_address",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="courts",
                to="crm.address",
                verbose_name="Адрес суда",
            ),
        ),
    ]
