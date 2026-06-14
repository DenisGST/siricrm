"""Демо-данные для раздела «Бухгалтерский учёт» — несколько входящих платежей,
чтобы посмотреть UI очереди/привязки до подключения коннекторов ТБанк.

    python manage.py accounting_demo          # создать (идемпотентно)
    python manage.py accounting_demo --clear  # удалить демо (external_id DEMO-*)

Демо-записи помечены external_id с префиксом DEMO-.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounting.models import IncomingPayment

DEMO = [
    dict(source="statement", external_id="DEMO-1", amount=Decimal("30000"),
         payer_name="Сидорова Е. В.", purpose="Оплата за Сидорова П. А., договор №БФЛ-1042"),
    dict(source="acquiring", external_id="DEMO-2", amount=Decimal("15000"),
         payer_name="Иванов Иван", payer_phone="+7 999 123-45-67", purpose="Оплата услуг (fo-y.ru)"),
    dict(source="acquiring", external_id="DEMO-3", amount=Decimal("8500"),
         payer_name="Петрва Аня", payer_phone="+7 912 000-11-22", purpose="Оплата услуг (fo-y.ru)"),
    dict(source="statement", external_id="DEMO-4", amount=Decimal("1240"),
         payer_name="ПАО Банк (проценты)", purpose="Начисление процентов на остаток"),
]


class Command(BaseCommand):
    help = "Демо входящих платежей для раздела «Бухгалтерский учёт»"

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="store_true", help="удалить демо-записи")

    def handle(self, *args, **opts):
        if opts["clear"]:
            n, _ = IncomingPayment.objects.filter(external_id__startswith="DEMO-").delete()
            self.stdout.write(self.style.SUCCESS(f"Удалено демо-записей: {n}"))
            return

        now = timezone.now()
        created = 0
        for i, row in enumerate(DEMO):
            _, was = IncomingPayment.objects.get_or_create(
                source=row["source"], external_id=row["external_id"],
                defaults={
                    "occurred_at": now - timedelta(hours=i * 5),
                    "amount": row["amount"],
                    "payer_name": row.get("payer_name", ""),
                    "payer_phone": row.get("payer_phone", ""),
                    "purpose": row.get("purpose", ""),
                },
            )
            created += int(was)
        self.stdout.write(self.style.SUCCESS(
            f"Демо готово (создано новых: {created}). Удалить: manage.py accounting_demo --clear"
        ))
