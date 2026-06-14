"""Бэкфилл is_settlement для уже загруженных входящих из выписки:
зачисление с плательщиком — банком-эквайером (АО «ТБанк») = сводный эквайринг.
"""
from django.db import migrations

ACQUIRER_INNS = {"7710140679"}  # АО «ТБанк» / Тинькофф Банк


def backfill(apps, schema_editor):
    IncomingPayment = apps.get_model("accounting", "IncomingPayment")
    for ip in IncomingPayment.objects.filter(source="statement", is_settlement=False):
        payer = (ip.raw or {}).get("payer") or {}
        name = (payer.get("name") or ip.payer_name or "").lower()
        inn = payer.get("inn") or ip.payer_inn or ""
        if inn in ACQUIRER_INNS or "тбанк" in name or "тинькофф" in name:
            ip.is_settlement = True
            ip.save(update_fields=["is_settlement"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0004_incomingpayment_is_settlement"),
    ]
    operations = [migrations.RunPython(backfill, noop)]
