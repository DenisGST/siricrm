from django.core.management.base import BaseCommand
from apps.telegram.userbot import client, import_message_history
import asyncio


class Command(BaseCommand):
    help = "Import message history from Telegram for a specific client"

    def add_arguments(self, parser):
        parser.add_argument('telegram_id', type=int, help='Telegram ID of the client')
        parser.add_argument('--limit', type=int, default=100, help='Number of messages to import')

    def handle(self, *args, **options):
        telegram_id = options['telegram_id']
        limit = options['limit']
        
        self.stdout.write(f"📥 Importing up to {limit} messages for client {telegram_id}...")
        
        async def run():
            await client.start(phone=options.get('phone') or '+79991234567')
            await import_message_history(telegram_id, limit)
            await client.disconnect()
        
        asyncio.run(run())
        self.stdout.write(self.style.SUCCESS("✅ Import completed"))
