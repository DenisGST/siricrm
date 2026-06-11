"""Сидинг шаблона заявления о банкротстве (для prod без shell). Идемпотентно."""
from django.db import migrations


def seed(apps, schema_editor):
    from apps.afd.isk_seed_data import DEFAULT_TEMPLATE_NAME, SECTIONS
    IskTemplate = apps.get_model("afd", "IskTemplate")
    IskSection = apps.get_model("afd", "IskSection")

    tpl, created = IskTemplate.objects.get_or_create(
        name=DEFAULT_TEMPLATE_NAME,
        defaults={"is_default": True, "is_active": True},
    )
    if not created and IskSection.objects.filter(template=tpl).exists():
        return
    for i, s in enumerate(SECTIONS):
        IskSection.objects.create(
            template=tpl, order=(i + 1) * 10,
            key=s.get("key", ""), title=s.get("title", ""),
            body=s.get("body", ""), block_type=s.get("block_type", "text"),
            align=s.get("align", "both"), bold=s.get("bold", False),
            is_optional=s.get("is_optional", False),
            include_condition=s.get("include_condition", ""),
        )


def unseed(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("afd", "0003_isktemplate_isksection")]
    operations = [migrations.RunPython(seed, unseed)]
