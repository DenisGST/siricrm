"""restore_db handler: восстановление БД ЭТОГО сервера из присланного дампа.

Вызывается агентом целевого окружения (обычно prod) из push_db, который запускается
на dev. Сначала — автоматический бэкап текущей БД (защита!), затем drop schema + restore.

ОПАСНО: полностью перезаписывает БД на этом сервере.
"""
import gzip
import os
import time
from pathlib import Path

import requests
from django.utils import timezone

from apps.devops.handlers.backup import run_backup
from apps.devops.handlers.pull_db import _download_from_s3, _post_restore_ensure_envs, _restore_dump
from apps.devops.tasks import register_handler

BACKUP_DIR = Path("/app/backups")


@register_handler("restore_db")
def run_restore_db(params: dict) -> dict:
    download_url = params.get("download_url")
    s3_bucket = params.get("s3_bucket")
    s3_key = params.get("s3_key")
    source_label = params.get("source_label") or "источник"
    safety_backup = params.get("safety_backup", True)

    if not download_url and not (s3_bucket and s3_key):
        raise ValueError("нужен download_url или (s3_bucket, s3_key)")

    log: list[str] = []
    result: dict = {"source": source_label}

    # 1. Защитный бэкап текущей БД
    if safety_backup:
        log.append("=== Защитный бэкап текущей БД перед перезаписью ===")
        try:
            b = run_backup({})
            log.append(b.get("output", ""))
            result["safety_backup"] = b.get("result", {})
        except Exception as e:
            # Бэкап не вышел — НЕ перезаписываем БД вслепую.
            raise RuntimeError(f"Защитный бэкап не удался, restore отменён: {e}")
    else:
        log.append("⚠ Защитный бэкап пропущен (safety_backup=False).")

    # 2. Скачиваем дамп
    log.append("\n=== Скачивание дампа ===")
    if download_url:
        resp = requests.get(download_url, timeout=300)
        resp.raise_for_status()
        gz_bytes = resp.content
    else:
        gz_bytes = _download_from_s3(s3_bucket, s3_key)
    log.append(f"  Скачано {len(gz_bytes):,} байт (gzip)")
    sql_bytes = gzip.decompress(gz_bytes)
    log.append(f"  Распаковано {len(sql_bytes):,} байт SQL")

    # Сохраняем присланный дамп локально (как «снимок источника»)
    BACKUP_DIR.mkdir(exist_ok=True)
    name = (s3_key or "incoming").rsplit("/", 1)[-1]
    if not name.endswith(".sql.gz"):
        name = f"incoming-{time.strftime('%Y%m%d-%H%M%S')}.sql.gz"
    local_path = BACKUP_DIR / name
    local_path.write_bytes(gz_bytes)
    log.append(f"  Сохранено локально: {local_path}")

    # 3. Restore
    log.append("\n=== ВНИМАНИЕ: drop schema public + restore ===")
    _restore_dump(sql_bytes, log)
    log.append("  Restore выполнен")

    # 4. Возвращаем Environment-записи (могли уехать вместе с дампом источника).
    _post_restore_ensure_envs(log)

    result.update({
        "s3_key": s3_key,
        "local_path": str(local_path),
        "size_bytes": len(gz_bytes),
        "size_mb": round(len(gz_bytes) / 2**20, 2),
        "finished_at": timezone.now().isoformat(),
    })
    return {"output": "\n".join(log), "result": result}
