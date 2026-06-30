from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0003_charge_bubble_id_payment_bubble_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="incometype",
            name="is_legal_services",
            field=models.BooleanField(
                default=False,
                help_text="Доход по этому типу — гонорар фирмы (юруслуги), а не транзит "
                "(госпошлина / публикации / доп.расходы). Используется в отчёте "
                "«Результаты работы отдела продаж».",
                verbose_name="Юруслуги (гонорар)",
            ),
        ),
    ]
