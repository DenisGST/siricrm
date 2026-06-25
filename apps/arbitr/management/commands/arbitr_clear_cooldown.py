"""Снять captcha-cooldown арбитр-парсера (для одного IP или для всех).

Cooldown теперь per-IP — если кто-то поймал капчу, замолкает только этот IP,
остальные runner'ы на других IP продолжают парсить.

  python manage.py arbitr_clear_cooldown                 # снять для ВСЕХ IP
  python manage.py arbitr_clear_cooldown --ip 1.2.3.4    # только один IP
"""
from django.core.management.base import BaseCommand

from apps.arbitr import cooldown


class Command(BaseCommand):
    help = "Снять per-IP captcha-cooldown арбитр-парсера."

    def add_arguments(self, parser):
        parser.add_argument(
            "--ip", default="",
            help="IP для снятия. Без флага — снять для всех IP.",
        )

    def handle(self, *args, **opts):
        ip = opts["ip"].strip()
        active = cooldown.all_active()
        if not active:
            self.stdout.write(self.style.WARNING("Активных cooldown'ов нет."))
            return
        if ip:
            if ip not in active:
                self.stdout.write(self.style.WARNING(f"IP {ip!r} не в cooldown'е."))
                return
            n = cooldown.clear(ip)
            self.stdout.write(self.style.SUCCESS(f"Снят cooldown для {ip}: {n} запись"))
            return
        n = cooldown.clear()
        self.stdout.write(self.style.SUCCESS(
            f"Снято cooldown'ов: {n} (были: {', '.join(active.keys())})"
        ))
