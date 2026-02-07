import random

from django.core.management.base import BaseCommand
from django.utils import timezone

from faker import Faker

from apps.crm.models import Client, Message, Operator


class Command(BaseCommand):
    help = "Generate test Clients and Messages"

    def add_arguments(self, parser):
        parser.add_argument(
            "--clients",
            type=int,
            default=100,
            help="Количество клиентов (по умолчанию 100)",
        )
        parser.add_argument(
            "--messages-per-client",
            type=int,
            default=200,
            help="Сообщений на клиента (по умолчанию 200)",
        )

    def handle(self, *args, **options):
        fake = Faker("ru_RU")
        num_clients = options["clients"]
        num_messages = options["messages_per_client"]

        self.stdout.write(self.style.NOTICE(
            f"Создаём {num_clients} клиентов и по {num_messages} сообщений каждому..."
        ))

        clients = []

        # 1. Генерируем клиентов
        for i in range(num_clients):
            first_name = fake.first_name()
            last_name = fake.last_name()
            username = fake.user_name()
            phone = fake.phone_number()

            client = Client.objects.create(
                telegram_id=10_000_000 + i,
                first_name=first_name,
                last_name=last_name,
                patronymic="",
                username=username,
                phone=phone,
                status=random.choice(["lead", "active", "inactive", "closed"]),
                last_message_at=None,
            )
            clients.append(client)

        self.stdout.write(self.style.SUCCESS(f"Создано клиентов: {len(clients)}"))

        # 2. Берём любого оператора (если есть)
        operator = Operator.objects.first()

        # 3. Генерируем сообщения
        total_msgs = 0
        for client in clients:
            messages = []
            now = timezone.now()

            for _ in range(num_messages):
                is_incoming = random.choice([True, False])
                direction = "incoming" if is_incoming else "outgoing"

                created_at = now - timezone.timedelta(
                    minutes=random.randint(0, 60 * 24 * 7)
                )

                msg = Message(
                    client=client,
                    operator=operator if not is_incoming else None,
                    message_type="text",
                    content=fake.sentence(nb_words=10),
                    direction=direction,
                    is_read=is_incoming and random.choice([True, False]),
                    created_at=created_at,
                    updated_at=created_at,
                )
                messages.append(msg)

            Message.objects.bulk_create(messages, batch_size=500)
            # обновляем last_message_at по последнему сообщению
            last_msg_time = max(m.created_at for m in messages)
            client.last_message_at = last_msg_time
            client.save(update_fields=["last_message_at"])

            total_msgs += len(messages)

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано сообщений: {total_msgs}"
        ))
