"""Регистрация webhook у Telegram Bot API для @Sirius_system_bot.

  python manage.py setup_telegram_leads_webhook              # установить
  python manage.py setup_telegram_leads_webhook --info       # показать текущий
  python manage.py setup_telegram_leads_webhook --delete     # удалить webhook

Берёт из env:
  TELEGRAM_BOT_TOKEN              — токен бота
  TELEGRAM_WEBHOOK_URL            — публичный URL CRM с конечным /, напр. https://siricrm.ru/
  TELEGRAM_LEADS_WEBHOOK_SECRET   — секретная часть пути

Итоговый webhook: <TELEGRAM_WEBHOOK_URL>telegram/leads-webhook/<secret>/
"""
import requests
from decouple import config
from django.core.management.base import BaseCommand, CommandError


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


class Command(BaseCommand):
    help = "Зарегистрировать/проверить/удалить Telegram webhook лидов"

    def add_arguments(self, parser):
        parser.add_argument("--info", action="store_true", help="getWebhookInfo")
        parser.add_argument("--delete", action="store_true", help="deleteWebhook")

    def handle(self, *args, **opts):
        token = config("TELEGRAM_BOT_TOKEN", default="")
        if not token:
            raise CommandError("TELEGRAM_BOT_TOKEN не задан в env")

        if opts["info"]:
            r = requests.get(_api(token, "getWebhookInfo"), timeout=15)
            self.stdout.write(r.text)
            return

        if opts["delete"]:
            r = requests.post(_api(token, "deleteWebhook"), timeout=15)
            self.stdout.write(r.text)
            return

        base = config("TELEGRAM_WEBHOOK_URL", default="").rstrip("/")
        secret = config("TELEGRAM_LEADS_WEBHOOK_SECRET", default="")
        if not base or not secret:
            raise CommandError(
                "Нужны TELEGRAM_WEBHOOK_URL и TELEGRAM_LEADS_WEBHOOK_SECRET"
            )

        # TELEGRAM_WEBHOOK_URL в env уже похож на siricrm.ru/telegram/webhook/ —
        # берём только origin (https://siricrm.ru) и собираем правильный путь.
        from urllib.parse import urlparse
        u = urlparse(base if base.startswith("http") else f"https://{base}")
        origin = f"{u.scheme}://{u.netloc}"
        url = f"{origin}/telegram/leads-webhook/{secret}/"

        r = requests.post(
            _api(token, "setWebhook"),
            json={
                "url": url,
                "allowed_updates": ["channel_post", "edited_channel_post", "message"],
                "drop_pending_updates": False,
            },
            timeout=15,
        )
        self.stdout.write(f"setWebhook → {url}")
        self.stdout.write(r.text)
