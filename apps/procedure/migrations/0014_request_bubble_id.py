from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("procedure", "0013_remove_arbitrationmanager_stamp_file_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="request",
            name="bubble_id",
            field=models.CharField(
                blank=True, db_index=True, help_text=(
                    "ID записи Сorrespondence в исходной CRM на bubble.io "
                    "(для идемпотентного импорта)."
                ),
                max_length=64, null=True, unique=True, verbose_name="Bubble ID",
            ),
        ),
    ]
