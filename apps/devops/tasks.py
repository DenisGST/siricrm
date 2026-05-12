"""Celery таски, выполняющие job'ы DevOps-агента.

Очередь: `devops` (см. CELERY_TASK_ROUTES в base settings).
Воркер запускается в контейнере devops-runner.
"""
import logging
import traceback
from datetime import datetime, timezone

from celery import shared_task
from django.utils import timezone as djtz

from .models import DevopsAgentJob

logger = logging.getLogger(__name__)


# Регистр обработчиков по action_type. Заполняется в подключаемых модулях:
# например, в apps/devops/handlers/status.py:
#     from apps.devops.tasks import register_handler
#     @register_handler("status")
#     def run_status(params): ...
HANDLERS: dict[str, callable] = {}


def register_handler(action_type: str):
    """Декоратор: регистрирует функцию-обработчик для action_type."""
    def deco(fn):
        HANDLERS[action_type] = fn
        return fn
    return deco


@shared_task(name="devops.run_agent_job", queue="devops")
def run_agent_job(job_id: str) -> dict:
    """Главная точка входа: берёт job по id и вызывает соответствующий handler.

    Handler возвращает dict с ключами:
      - output: str — текстовый лог
      - result: dict — структурированный результат
    """
    try:
        job = DevopsAgentJob.objects.get(id=job_id)
    except DevopsAgentJob.DoesNotExist:
        logger.error("DevopsAgentJob %s not found", job_id)
        return {"error": "not_found"}

    job.status = DevopsAgentJob.Status.RUNNING
    job.save(update_fields=["status"])

    handler = HANDLERS.get(job.action_type)
    if handler is None:
        job.status = DevopsAgentJob.Status.FAILED
        job.output = f"Неизвестный action_type: {job.action_type}\n"
        job.finished_at = djtz.now()
        job.save(update_fields=["status", "output", "finished_at"])
        return {"error": "unknown_action"}

    try:
        result = handler(job.params or {}) or {}
        job.output = (result.get("output") or "")[:200_000]
        job.result = result.get("result") or {}
        job.status = DevopsAgentJob.Status.DONE
    except Exception:
        job.status = DevopsAgentJob.Status.FAILED
        job.output = (job.output or "") + "\n" + traceback.format_exc()
    finally:
        job.finished_at = djtz.now()
        job.save(update_fields=["status", "output", "result", "finished_at"])

    return {"status": job.status, "job_id": str(job.pk)}
