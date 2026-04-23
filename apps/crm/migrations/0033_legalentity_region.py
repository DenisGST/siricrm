from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0032_seed_kind_zags"),
    ]

    operations = [
        migrations.AddField(
            model_name="legalentity",
            name="region",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="legal_entities",
                to="crm.region",
                verbose_name="Регион (субъект РФ)",
            ),
        ),
    ]
