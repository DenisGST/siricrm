"""passport_line раздел иска → block_type court_header (смещение шапки вправо)."""
from django.db import migrations


def fix(apps, schema_editor):
    IskSection = apps.get_model("afd", "IskSection")
    IskSection.objects.filter(key="passport_line", block_type="text").update(
        block_type="court_header"
    )


def back(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("afd", "0004_seed_isk")]
    operations = [migrations.RunPython(fix, back)]
