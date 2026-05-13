"""pull_tables: оркестратор для prod → dev selective sync.

Запускается ЛОКАЛЬНО на dev-сервере. Делает:
  1. AgentClient(source).create_job('dumpdata_tables', ...) → ждём presigned URL
  2. loaddata_tables локально (этот же runner) — UPSERT в текущую dev-БД
  3. возвращает агрегированный результат

В отличие от pull_db (drop schema всего public) — это аккуратный UPSERT
по выбранным таблицам; остальная dev-БД не трогается.
"""
from django.utils import timezone

from apps.devops.agent_client import AgentClient
from apps.devops.handlers.loaddata_tables import run_loaddata_tables
from apps.devops.handlers.pull_db import _wait_for_remote_job
from apps.devops.models import Environment
from apps.devops.tasks import register_handler


@register_handler("pull_tables")
def run_pull_tables(params: dict) -> dict:
    source_env_id = params.get("source_env_id")
    if not source_env_id:
        raise ValueError("source_env_id required in params")
    models = params.get("models") or []
    if not models:
        raise ValueError("models list пуст")

    source_env = Environment.objects.get(pk=source_env_id)
    log = [f"Источник: {source_env.name} ({source_env.base_url})"]
    log.append(f"Моделей к выгрузке: {len(models)}")

    # 1. Просим источника выгрузить таблицы
    log.append(f"\n=== Шаг 1: dumpdata на {source_env.name} ===")
    client = AgentClient(source_env)
    remote_job = client.create_job("dumpdata_tables", params={"models": models})
    remote_job_id = remote_job["id"]
    log.append(f"  remote job: {remote_job_id}")

    log.append("Ожидание...")
    finished = _wait_for_remote_job(client, remote_job_id, log, timeout_sec=600)
    dump_result = finished.get("result") or {}
    download_url = dump_result.get("download_url")
    if not download_url:
        raise RuntimeError(f"источник не вернул download_url: {dump_result}")
    log.append(f"  Фикстура готова: {dump_result.get('size_mb')} MB, "
               f"объектов: {dump_result.get('object_count')}")

    # 2. Локальный loaddata на dev
    log.append("\n=== Шаг 2: loaddata здесь (dev) ===")
    load = run_loaddata_tables({
        "download_url": download_url,
        "s3_bucket": dump_result.get("s3_bucket"),
        "s3_key": dump_result.get("s3_key"),
        "models": models,
        "source_label": f"{source_env.name} (через pull_tables)",
        "safety_backup": True,
    })
    log.append(load["output"])
    load_result = load["result"]

    return {
        "output": "\n".join(log),
        "result": {
            "source_env": source_env.name,
            "models": models,
            "dump": dump_result,
            "load": load_result,
            "finished_at": timezone.now().isoformat(),
        },
    }
