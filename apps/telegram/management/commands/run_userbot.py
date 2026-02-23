from django.core.management.base import BaseCommand
from apps.telegram.userbot import run_userbot


class Command(BaseCommand):
    help = "Run Telegram userbot for read receipts and enhanced features"

    def handle(self, *args, **options):
        self.stdout.write("🚀 Starting Telegram userbot...")
        run_userbot()
