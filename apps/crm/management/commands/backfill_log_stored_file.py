"""Бэкофилл ClientLogEntry.stored_file для исторических file-событий.

До появления FK `ClientLogEntry.stored_file` события «Получен файл»
(event `file_received`) и действия «Отправлен файл» (action `file_sent`)
писались только текстовым комментом вида
«Telegram: получен файл — <имя>». Этот FK был добавлен миграцией
`crm.0073`, но старые записи остались без привязки.

Команда сопоставляет каждую такую запись с реальным `Message`
(того же клиента, того же направления, в окне ±N минут) и копирует
`Message.file` в `ClientLogEntry.stored_file`. Приоритет — точное
совпадение имени файла из коммента; при отсутствии имени (комменты
вида «получен файл (image)») берётся ближайшее по времени сообщение.

Идемпотентна: трогает только записи с `stored_file IS NULL`.

    python manage.py backfill_log_stored_file [--dry-run] [--window 10]
"""
import datetime
import re

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.crm.models import ClientLogEntry, Message


_FNAME_RE = re.compile(r"—\s*(.+)$")


def _filename_from_comment(comment: str):
    """Извлечь имя файла из коммента «… — <имя>». None, если нет."""
    m = _FNAME_RE.search(comment or "")
    return m.group(1).strip() if m else None


class Command(BaseCommand):
    help = "Привязать StoredFile к историческим записям лога «Получен/Отправлен файл»"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Только показать, что было бы сделано, без записи в БД.",
        )
        parser.add_argument(
            "--window", type=int, default=10,
            help="Окно поиска сообщения вокруг времени записи, минут (по умолчанию 10).",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        window = datetime.timedelta(minutes=opts["window"])

        qs = ClientLogEntry.objects.filter(stored_file__isnull=True).filter(
            Q(event_type__code="file_received") | Q(action_type__code="file_sent")
        ).order_by("created_at")

        total = qs.count()
        self.stdout.write(f"Записей без stored_file: {total} (окно ±{opts['window']} мин){' [DRY-RUN]' if dry else ''}")

        matched_name = matched_time = unmatched = 0
        # Чтобы один и тот же Message не привязался к двум разным записям —
        # ведём set уже задействованных message.id (в рамках прогона).
        used_msg_ids: set = set()

        for e in qs.iterator():
            direction = "incoming" if e.kind == "event" else "outgoing"
            fname = _filename_from_comment(e.comment)

            cand = list(
                Message.objects.filter(
                    client_id=e.client_id, file__isnull=False, direction=direction,
                    created_at__gte=e.created_at - window,
                    created_at__lte=e.created_at + window,
                ).select_related("file")
            )

            def delta(m):
                return abs((m.created_at - e.created_at).total_seconds())

            chosen = None
            chosen_kind = None

            if fname:
                name_hits = [
                    m for m in cand
                    if fname == (m.file.filename or "") or fname == (m.file_name or "")
                ]
                name_hits = [m for m in name_hits if m.id not in used_msg_ids]
                if name_hits:
                    chosen = min(name_hits, key=delta)
                    chosen_kind = "name"

            if chosen is None:
                free = [m for m in cand if m.id not in used_msg_ids]
                if free:
                    chosen = min(free, key=delta)
                    chosen_kind = "time"

            if chosen is None:
                unmatched += 1
                self.stdout.write(self.style.WARNING(
                    f"  ✗ не найдено: client={e.client_id} {direction} "
                    f"{e.created_at:%Y-%m-%d %H:%M} «{(e.comment or '')[:50]}»"
                ))
                continue

            used_msg_ids.add(chosen.id)
            if chosen_kind == "name":
                matched_name += 1
            else:
                matched_time += 1

            if not dry:
                e.stored_file = chosen.file
                e.save(update_fields=["stored_file"])

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Готово{' (dry-run)' if dry else ''}: по имени={matched_name}, "
            f"по времени={matched_time}, не найдено={unmatched}, всего={total}"
        ))
