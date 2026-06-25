"""Перепустить зависшие исходящие сообщения (is_sent=False AND is_failed=False
дольше N минут) через соответствующие send-таски.

Появилось после инцидента 24.06.2026 с DNS на проде — 5 сообщений (3 WA + 2 MAX)
повисли в ⏳, ретраи Celery exhauste'd без is_failed=True. Сейчас exhaustion уже
помечает is_failed (apps/whatsapp/tasks.py, apps/maxchat/tasks.py, apps/crm/tasks.py),
но команда остаётся полезной — для массового восстановления после сетевого сбоя
и как защита от любых будущих тихих зависаний.

Использование:
    python manage.py retry_stuck_messages              # dry-run (показать что нашёл)
    python manage.py retry_stuck_messages --apply      # реально переотправить
    python manage.py retry_stuck_messages --minutes 30 # порог — старше 30 мин
    python manage.py retry_stuck_messages --channel max --apply

🛑 По умолчанию команда смотрит только окно [5 мин ... 24 часа] и игнорирует
сообщения без employee — это защита от перепосылки клиентам легаси-импорта
из bubble (где `is_sent=False` массово, без отправителя, возраст недели/месяцы).
Если действительно нужно ретраить очень старое или системное — явно укажи
`--max-hours 9999 --include-no-employee`.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


CHANNEL_TASKS = {
    "whatsapp": ("apps.whatsapp.tasks", "send_whatsapp_message_task"),
    "max": ("apps.maxchat.tasks", "send_max_message_task"),
    "telegram": ("apps.crm.tasks", "send_telegram_message_task"),
}


class Command(BaseCommand):
    help = "Найти зависшие исходящие WA/MAX/TG (⏳) и переотправить через send-таски"

    def add_arguments(self, parser):
        parser.add_argument(
            "--minutes", type=int, default=5,
            help="Минимальный возраст: считать зависшим всё, что висит дольше N минут (default 5)",
        )
        parser.add_argument(
            "--max-hours", type=int, default=24,
            help="ВЕРХНИЙ потолок возраста (default 24ч). Защита от перепосылки старого "
                 "легаси-импорта. Поставь 9999 чтобы снять.",
        )
        parser.add_argument(
            "--include-no-employee", action="store_true",
            help="Включать сообщения без employee (default — игнорируются как импорт)",
        )
        parser.add_argument(
            "--channel", choices=list(CHANNEL_TASKS), default=None,
            help="Только указанный канал (whatsapp/max/telegram). По умолчанию — все.",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Реально дёрнуть таски. Без флага — dry-run (только вывести список).",
        )
        parser.add_argument(
            "--limit", type=int, default=500,
            help="Жёсткий лимит на количество сообщений за один прогон (default 500)",
        )

    def handle(self, *_, minutes, max_hours, include_no_employee, channel, apply, limit, **__):
        from apps.crm.models import Message  # ленивый импорт (apps loaded)

        now = timezone.now()
        upper = now - timedelta(minutes=minutes)
        lower = now - timedelta(hours=max_hours)
        qs = Message.objects.filter(
            direction="outgoing",
            is_sent=False,
            is_failed=False,
            created_at__lt=upper,
            created_at__gte=lower,
        )
        if not include_no_employee:
            qs = qs.filter(employee__isnull=False)
        if channel:
            qs = qs.filter(channel=channel)
        qs = qs.order_by("created_at")[:limit]

        total = qs.count()
        if not total:
            self.stdout.write(self.style.SUCCESS("Зависших сообщений нет."))
            return

        self.stdout.write(
            f"Найдено зависших: {total} (старше {minutes} минут)"
            + (f", канал={channel}" if channel else "")
        )

        # Группируем по каналу для удобства
        by_channel: dict[str, list] = {}
        for msg in qs:
            by_channel.setdefault(msg.channel or "?", []).append(msg)

        for ch, msgs in by_channel.items():
            self.stdout.write(f"\n--- {ch.upper()}: {len(msgs)} ---")
            for m in msgs[:20]:
                ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                emp = (m.employee and m.employee.user.username) or "—"
                cli_name = ""
                if m.client:
                    cli_name = (
                        f"{m.client.last_name or ''} {m.client.first_name or ''}"
                    ).strip() or str(m.client_id)
                preview = (m.content or "")[:60].replace("\n", " ")
                self.stdout.write(
                    f"  {m.id} | {ts} | от {emp} → {cli_name} | {preview!r}"
                )
            if len(msgs) > 20:
                self.stdout.write(f"  ... ещё {len(msgs) - 20}")

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\n(dry-run) Без --apply ничего не отправляется. Запусти ещё раз с --apply."
            ))
            return

        # Реальный ретрай
        sent = skipped = 0
        for ch, msgs in by_channel.items():
            task_info = CHANNEL_TASKS.get(ch)
            if not task_info:
                self.stdout.write(self.style.WARNING(
                    f"  {ch}: нет send-таски в реестре, пропущено {len(msgs)}"
                ))
                skipped += len(msgs)
                continue
            mod_name, fn_name = task_info
            from importlib import import_module
            mod = import_module(mod_name)
            task_fn = getattr(mod, fn_name)
            for m in msgs:
                try:
                    task_fn.delay(str(m.id))
                    sent += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(
                        f"  {m.id} ({ch}): не удалось поставить в очередь: {e}"
                    ))
                    skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nГотово: поставлено в очередь {sent}, пропущено {skipped}."
            f" Проверь is_sent/is_failed через минуту."
        ))
