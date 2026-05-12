"""push_db handler: залить БД ЭТОГО сервера (dev) в целевое окружение (prod).

Запускается ЛОКАЛЬНО на dev (в его devops-runner). Шаги:
  1. Бэкап dev-БД → S3 → pre-signed download URL (переиспользуем backup handler).
  2. Шлём целевому агенту job 'restore_db' с этим URL (target сам сделает защитный
     бэкап своей БД, затем drop schema + restore).
  3. Ждём завершения, возвращаем сводный отчёт.

ОПАСНО: перезаписывает БД на целевом сервере (обычно боевом).
"""
from django.utils import timezone

from apps.devops.agent_client import AgentClient
from apps.devops.handlers.backup import run_backup
from apps.devops.handlers.pull_db import _wait_for_remote_job
from apps.devops.models import Environment
from apps.devops.tasks import register_handler


@register_handler("push_db")
def run_push_db(params: dict) -> dict:
    target_env_id = params.get("target_env_id")
    if not target_env_id:
        raise ValueError("target_env_id обязателен")
    target = Environment.objects.get(pk=target_env_id)

    log = [f"Цель: {target.name} ({target.base_url})"]

    # 1. Бэкап текущей (dev) БД
    log.append("\n=== Бэкап dev-БД ===")
    b = run_backup({})
    log.append(b.get("output", ""))
    res = b.get("result") or {}
    download_url = res.get("download_url")
    if not download_url:
        raise RuntimeError("backup не вернул download_url — restore на цели невозможен")

    # 2. Просим целевой агент восстановиться из этого дампа
    log.append(f"\n=== Отправка restore_db на {target.name} ===")
    client = AgentClient(target)
    remote_job = client.create_job("restore_db", params={
        "download_url": download_url,
        "s3_bucket": res.get("s3_bucket"),
        "s3_key": res.get("s3_key"),
        "source_label": "dev",
        "safety_backup": True,
    })
    rjid = remote_job["id"]
    log.append(f"  remote job: {rjid}")

    # 3. Ждём (на цели: защитный бэкап + скачивание + restore)
    log.append("Ожидание restore на цели (защитный бэкап + restore)...")
    finished = _wait_for_remote_job(client, rjid, log, timeout_sec=1200)
    log.append("  restore на цели завершён")
    remote_out = finished.get("output") or ""
    if remote_out:
        log.append("\n----- лог цели -----\n" + remote_out)

    return {
        "output": "\n".join(log),
        "result": {
            "target_env": target.name,
            "dump": {k: res.get(k) for k in ("filename", "s3_key", "size_mb")},
            "remote_result": finished.get("result"),
            "finished_at": timezone.now().isoformat(),
        },
    }
