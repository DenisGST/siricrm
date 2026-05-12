"""Создаёт стандартные Environment-записи."""
from django.core.management.base import BaseCommand

from apps.devops.models import Environment


class Command(BaseCommand):
    help = "Initialize Environment rows for DevOps panel"

    def handle(self, *args, **opts):
        # 'self' — указывает на этот же сервер, для тестирования и для случая когда
        # dev и prod пока на одной машине.
        env, created = Environment.objects.update_or_create(
            name="self",
            defaults={
                "base_url": "https://crmsiri.ru",
                "agent_token_env": "DEVOPS_AGENT_TOKEN",
                "is_active": True,
            },
        )
        verb = "создано" if created else "обновлено"
        self.stdout.write(self.style.SUCCESS(f"Environment 'self' {verb}: {env.base_url}"))

        # 'prod' — будущий боевой сервер. Пока is_active=False до развертывания.
        env, created = Environment.objects.update_or_create(
            name="prod",
            defaults={
                "base_url": "https://siricrm.ru",
                "agent_token_env": "DEVOPS_AGENT_TOKEN_PROD",
                "is_active": False,
            },
        )
        verb = "создано" if created else "обновлено"
        self.stdout.write(self.style.SUCCESS(f"Environment 'prod' {verb}: {env.base_url} (inactive)"))
