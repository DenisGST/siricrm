"""Бэкофилл ClientLogEntry.stored_file для исторических file-событий.

До FK `stored_file` (миграция 0073) события «Получен файл» (file_received)
и действия «Отправлен файл» (file_sent) писались только текстовым
комментом «Telegram: получен файл — <имя>». Здесь привязываем к ним
реальный StoredFile, сопоставляя с Message (тот же клиент + направление
+ окно по времени, приоритет — точное совпадение имени файла).

Логика повторяет management-команду `backfill_log_stored_file`
(оставлена для ручного повторного прогона / новых окружений).
Идемпотентна: трогает только записи с stored_file IS NULL — на уже
обработанном окружении (dev) найдёт 0 и ничего не сделает.
"""
import datetime
import re

from django.db import migrations
from django.db.models import Q


_FNAME_RE = re.compile(r"—\s*(.+)$")
_WINDOW = datetime.timedelta(minutes=10)


def _filename_from_comment(comment):
    m = _FNAME_RE.search(comment or "")
    return m.group(1).strip() if m else None


def backfill(apps, schema_editor):
    ClientLogEntry = apps.get_model("crm", "ClientLogEntry")
    Message = apps.get_model("crm", "Message")

    qs = ClientLogEntry.objects.filter(stored_file__isnull=True).filter(
        Q(event_type__code="file_received") | Q(action_type__code="file_sent")
    ).order_by("created_at")

    used_msg_ids = set()
    linked = 0

    for e in qs.iterator():
        direction = "incoming" if e.kind == "event" else "outgoing"
        fname = _filename_from_comment(e.comment)

        cand = list(
            Message.objects.filter(
                client_id=e.client_id, file__isnull=False, direction=direction,
                created_at__gte=e.created_at - _WINDOW,
                created_at__lte=e.created_at + _WINDOW,
            ).select_related("file")
        )

        def delta(m):
            return abs((m.created_at - e.created_at).total_seconds())

        chosen = None
        if fname:
            hits = [
                m for m in cand
                if m.id not in used_msg_ids
                and (fname == (m.file.filename or "") or fname == (m.file_name or ""))
            ]
            if hits:
                chosen = min(hits, key=delta)
        if chosen is None:
            free = [m for m in cand if m.id not in used_msg_ids]
            if free:
                chosen = min(free, key=delta)

        if chosen is not None:
            used_msg_ids.add(chosen.id)
            e.stored_file_id = chosen.file_id
            e.save(update_fields=["stored_file"])
            linked += 1

    print(f"  backfill_log_stored_file: привязано {linked} записей")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0073_clientlogentry_stored_file'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
