#!/usr/bin/env python3
"""Удаление архива голосовых с АТС (решение владельца 25.08.2026).

🛑 Набор собираем по ФАКТУ — mtime файла и состояние переноса, — а НЕ по
пометке в учёте агента: массовым update ранее туда попали и свежие файлы,
включая сообщение того же дня. Опираться на собственную пометку при
безвозвратном удалении нельзя.

🛑 Не трогаем: переданное в CRM и всё свежее горизонта.
Откат: файлы лежат в суточных tar-архивах /records/backup (30 дней).
"""
import os
import sqlite3
import sys
import time

ROOT = "/var/spool/asterisk/monitor"
STATE = "/var/lib/pbx-agent/state.db"
HORIZON_DAYS = 2

conn = sqlite3.connect(STATE)
transferred = {
    row[0] for row in conn.execute(
        "select filename from voicemails "
        "where sent = 1 and (last_error is null or length(last_error) = 0)")
}
horizon = time.time() - HORIZON_DAYS * 86400

to_delete, freed, oldest, newest = [], 0, None, None
for name in sorted(os.listdir(ROOT)):
    if not name.lower().endswith(".wav") or name in transferred:
        continue
    path = os.path.join(ROOT, name)
    try:
        st = os.stat(path)
    except OSError:
        continue
    if st.st_mtime >= horizon:
        continue
    to_delete.append(name)
    freed += st.st_size
    oldest = min(oldest or st.st_mtime, st.st_mtime)
    newest = max(newest or st.st_mtime, st.st_mtime)

fmt = "%Y-%m-%d"
print(f"под удаление : {len(to_delete)} файлов, {freed / 2**20:.1f} МБ")
if to_delete:
    print(f"период       : {time.strftime(fmt, time.localtime(oldest))} … "
          f"{time.strftime(fmt, time.localtime(newest))}")
print(f"передано в CRM (не трогаем): {len(transferred)}")
print(f"остальные свежее {HORIZON_DAYS} сут — не трогаем")

if "--apply" not in sys.argv:
    print("\n[dry-run] ничего не удалено; для удаления — --apply")
    raise SystemExit(0)

removed = 0
for name in to_delete:
    try:
        os.unlink(os.path.join(ROOT, name))
        removed += 1
    except OSError as exc:
        print(f"  не удалось удалить {name}: {exc}")
conn.executemany("update voicemails set wav_deleted = 1 where filename = ?",
                 [(n,) for n in to_delete])
conn.commit()
print(f"\nудалено: {removed} из {len(to_delete)}")
