"""Снять глобальный cooldown арбитр-парсера (после капчи от kad).

Если решил капчу в браузере раньше истечения 12ч — этой командой
сообщи парсеру, что можно возобновлять работу.

  python manage.py arbitr_clear_cooldown
"""
from django.core.management.base import BaseCommand

from apps.arbitr import cooldown


class Command(BaseCommand):
    help = "Снять глобальный captcha-cooldown арбитр-парсера."

    def handle(self, *args, **opts):
        until = cooldown.until()
        if until is None:
            self.stdout.write(self.style.WARNING("Cooldown не активен — нечего снимать."))
            return
        cooldown.clear()
        self.stdout.write(self.style.SUCCESS(
            f"Cooldown снят (был активен до {until:%d.%m %H:%M} МСК)."
        ))
