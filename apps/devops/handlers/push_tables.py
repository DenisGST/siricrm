"""push_tables: оркестратор для dev → prod selective sync.

Запускается ЛОКАЛЬНО на dev-сервере (в его devops-runner). Делает:
  1. dumpdata_tables на dev (этот же runner) → JSON.gz в S3 + presigned URL
  2. AgentClient(target).create_job('loaddata_tables', ...) → ждём done
  3. возвращает агрегированный результат

UPSERT-семантика: на цели для каждой записи с тем же PK — UPDATE; новой — INSERT.
Строки которых нет в фикстуре — на цели НЕ удаляются.
"""
import time

from django.utils import timezone

from apps.devops.agent_client import AgentClient
from apps.devops.handlers.dumpdata_tables import run_dumpdata_tables
from apps.devops.handlers.pull_db import _wait_for_remote_job
from apps.devops.models import Environment
from apps.devops.tasks import register_handler


@register_handler("push_tables")
def run_push_tables(params: dict) -> dict:
    target_env_id = params.get("target_env_id")
    if not target_env_id:
        raise ValueError("target_env_id required in params")
    models = params.get("models") or []
    if not models:
        raise ValueError("models list пуст")

    target_env = Environment.objects.get(pk=target_env_id)
    log = [f"Цель: {target_env.name} ({target_env.base_url})"]
    log.append(f"Моделей к выгрузке: {len(models)}")

    # 1. Локальный dumpdata (тот же dev-runner)
    log.append("\n=== Шаг 1: dumpdata здесь (dev) ===")
    dump = run_dumpdata_tables({"models": models})
    log.append(dump["output"])
    dump_result = dump["result"]
    download_url = dump_result.get("download_url")
    if not download_url:
        raise RuntimeError("dumpdata не вернул download_url")

    # 2. Просим целевого агента применить через loaddata
    log.append(f"\n=== Шаг 2: loaddata на {target_env.name} ===")
    client = AgentClient(target_env)
    remote_job = client.create_job(
        "loaddata_tables",
        params={
            "download_url": download_url,
            "s3_bucket": dump_result.get("s3_bucket"),
            "s3_key": dump_result.get("s3_key"),
            "models": models,
            "source_label": "dev (через push_tables)",
            "safety_backup": True,
        },
    )
    remote_job_id = remote_job["id"]
    log.append(f"  remote job: {remote_job_id}")

    log.append("Ожидание завершения...")
    finished = _wait_for_remote_job(client, remote_job_id, log, timeout_sec=900)
    remote_result = finished.get("result") or {}
    log.append("  Готово.")

    return {
        "output": "\n".join(log),
        "result": {
            "target_env": target_env.name,
            "models": models,
            "dump": dump_result,
            "load": remote_result,
            "finished_at": timezone.now().isoformat(),
        },
    }
