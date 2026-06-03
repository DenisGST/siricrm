from datetime import timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from apps.core.models import Employee
from apps.crm.models import Client, ClientLogEntry, Message
from apps.crm import client_log


class Command(BaseCommand):
    help = "Для клиентов без записи first_contact — создаёт её; timestamp = 1 сек до первого сообщения"

    def handle(self, *args, **options):
        bot_user, _ = User.objects.get_or_create(
            username="sirius_bot",
            defaults={"first_name": "Бот", "last_name": "Сириус", "is_active": False},
        )
        bot_emp, _ = Employee.objects.get_or_create(user=bot_user)

        # Клиенты у которых уже есть event first_contact — пропускаем
        clients_with_event = set(
            ClientLogEntry.objects
            .filter(kind="event", event_type__code="first_contact")
            .values_list("client_id", flat=True)
        )
        clients_without = Client.objects.exclude(id__in=clients_with_event)
        total = clients_without.count()
        self.stdout.write(f"Клиентов без записи first_contact: {total}")

        created_count = 0
        fixed_ts_count = 0
        no_msg_count = 0

        for client in clients_without.iterator():
            if client.telegram_id:
                description = "Первое обращение через Telegram"
            elif client.max_chat_id:
                description = "Первое обращение через MAX"
            else:
                description = "Клиент создан в системе"

            entry = client_log.record_event(
                client, "first_contact",
                comment=description, employee=bot_emp,
            )
            if entry is None:
                continue
            created_count += 1

            # Устанавливаем дату на 1 секунду раньше первого сообщения
            first_msg = (
                Message.objects.filter(client=client)
                .order_by("created_at")
                .values("created_at")
                .first()
            )
            if first_msg:
                new_ts = first_msg["created_at"] - timedelta(seconds=1)
                ClientLogEntry.objects.filter(pk=entry.pk).update(created_at=new_ts)
                fixed_ts_count += 1
            else:
                no_msg_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"✅ Создано: {created_count} | "
            f"Дата по сообщению: {fixed_ts_count} | "
            f"Без сообщений (текущее время): {no_msg_count}"
        ))

        # Исправляем уже существующие записи sirius_bot без корректной даты
        self.stdout.write("\nПроверяем существующие записи first_contact (sirius_bot)...")
        existing = ClientLogEntry.objects.filter(
            kind="event", event_type__code="first_contact", employee=bot_emp,
        )
        fixed = 0
        for ev in existing.iterator():
            first_msg = (
                Message.objects.filter(client=ev.client)
                .order_by("created_at")
                .values("created_at")
                .first()
            )
            if first_msg:
                new_ts = first_msg["created_at"] - timedelta(seconds=1)
                if abs((ev.created_at - new_ts).total_seconds()) > 2:
                    ClientLogEntry.objects.filter(pk=ev.pk).update(created_at=new_ts)
                    fixed += 1
        self.stdout.write(self.style.SUCCESS(f"✅ Исправлено временных меток: {fixed}"))
