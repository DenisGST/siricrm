"""Сидинг счёта прихода «Эквайринг» (по решению — эквайринг учитывается
на отдельном счёте, не на общем р/с). Идемпотентно."""
from django.db import migrations


def seed(apps, schema_editor):
    IncomingAccount = apps.get_model("finance", "IncomingAccount")
    IncomingAccount.objects.get_or_create(
        account_type="bank",
        name="Эквайринг",
        defaults={"is_active": True},
    )


def unseed(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0002_initial"),
        ("finance", "0001_initial"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
