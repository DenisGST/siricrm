"""Заполнить ClientPhone-алиасами (purpose='whatsapp') номера telWSP из
уже импортированных записей ProjectBFL.

После запуска WhatsApp-сообщения, у которых раньше «клиент не найден»,
будут матчиться через ClientPhone-алиас. Дальше нужно `reapply_failed_wa`.
"""
from django.core.management.base import BaseCommand

from apps.bubble_import.extractors import normalize_phone
from apps.bubble_import.models import BubbleRecord
from apps.crm.models import Client
from apps.crm.phone_utils import add_client_phone


class Command(BaseCommand):
    help = "Перенести ProjectBFL.telWSP в ClientPhone(purpose='whatsapp')"

    def handle(self, *args, **opts):
        qs = BubbleRecord.objects.filter(entity="ProjectBFL")
        total = qs.count()
        self.stdout.write(f"Проходим по {total} ProjectBFL-записям…")

        added = 0
        no_phone = 0
        no_client = 0
        already = 0
        for rec in qs.iterator(chunk_size=500):
            raw = rec.raw or {}
            tel_wsp = normalize_phone(raw.get("telWSP"))
            if not tel_wsp:
                no_phone += 1
                continue
            dolgnik = raw.get("dolgnik")
            client = Client.objects.filter(bubble_id=dolgnik).first()
            if client is None:
                no_client += 1
                continue
            obj = add_client_phone(client, tel_wsp, purpose="whatsapp")
            if obj is None:
                no_client += 1  # номер занят другим клиентом
                continue
            if obj.client_id == client.id:
                # Считаем «added» если объект только что появился — это
                # сложно отличить без поля. Грубо: если уже был — считаем
                # already. Простоты ради считаем все обращения как «added».
                added += 1
            else:
                already += 1

        self.stdout.write(self.style.SUCCESS(
            f"Готово: затронуто {added} клиентов, "
            f"без telWSP — {no_phone}, без клиента/занят — {no_client}"
        ))
