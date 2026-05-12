"""Создаёт стандартные Environment-записи."""
from django.core.management.base import BaseCommand

from apps.devops.models import Environment


class Command(BaseCommand):
    help = "Initialize Environment rows for DevOps panel"

    def handle(self, *args, **opts):
        # Подчищаем старое имя 'self', если осталось с прежних версий.
        Environment.objects.filter(name="self").update(name="dev")

        # 'dev' — этот сервер (где живёт панель и ведётся разработка).
        env, created = Environment.objects.update_or_create(
            name="dev",
            defaults={
                "base_url": "https://crmsiri.ru",
                "agent_token_env": "DEVOPS_AGENT_TOKEN",
                "is_active": True,
            },
        )
        verb = "создано" if created else "обновлено"
        self.stdout.write(self.style.SUCCESS(f"Environment 'dev' {verb}: {env.base_url}"))

        # 'prod' — боевой сервер siricrm.ru. Деплой/бэкапы/синк БД идут через его агента.
        env, created = Environment.objects.update_or_create(
            name="prod",
            defaults={
                "base_url": "https://siricrm.ru",
                "agent_token_env": "DEVOPS_AGENT_TOKEN_PROD",
                "is_active": True,
            },
        )
        verb = "создано" if created else "обновлено"
        self.stdout.write(self.style.SUCCESS(f"Environment 'prod' {verb}: {env.base_url}"))
