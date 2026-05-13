"""Celery таски, выполняющие job'ы DevOps-агента.

Очередь: `devops` (см. CELERY_TASK_ROUTES в base settings).
Воркер запускается в контейнере devops-runner.
"""
import logging
import traceback

from celery import shared_task
from django.utils import timezone as djtz

from .agent_client import AgentClient
from .models import DevopsAction, DevopsAgentJob

logger = logging.getLogger(__name__)


# Action types, выполняемые ЛОКАЛЬНО на dev-сервере (в devops-runner этого сервера),
# а не через агента целевого окружения. Значение — функция env → стартовые params.
# Лежит здесь (а не в views.py), потому что нужна и при синке статуса в фоне.
LOCAL_ACTIONS: dict[str, callable] = {
    # затянуть БД источника СЮДА (на dev): источник = окружение, на котором нажали
    "pull_db": lambda env: {"source_env_id": env.id},
    # залить ЭТУ БД (dev) в целевое окружение: цель = окружение, на котором нажали
    "push_db": lambda env: {"target_env_id": env.id},
}


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

    # Пробрасываем job_id в params, чтобы handler мог self-aware (нужно для
    # pull_db/restore_db — они затирают свою же tracking-запись, и им нужно её
    # снапшотнуть до restore'а и восстановить после).
    handler_params = {**(job.params or {}), "__job_id__": str(job.pk)}

    try:
        result = handler(handler_params) or {}
        job.output = (result.get("output") or "")[:200_000]
        job.result = result.get("result") or {}
        job.status = DevopsAgentJob.Status.DONE
    except Exception:
        job.status = DevopsAgentJob.Status.FAILED
        job.output = (job.output or "") + "\n" + traceback.format_exc()
    finally:
        job.finished_at = djtz.now()
        # update_or_create вместо save() — устойчиво к случаю, когда строка
        # была дропнута самим handler'ом (pull_db/restore_db) и не восстановлена
        # его snapshot'ом по какой-то причине.
        DevopsAgentJob.objects.update_or_create(
            id=job.pk,
            defaults={
                "action_type": job.action_type,
                "params": job.params or {},
                "status": job.status,
                "output": job.output or "",
                "result": job.result or {},
                "finished_at": job.finished_at,
            },
        )

    return {"status": job.status, "job_id": str(job.pk)}


# ============================================================================
# Фоновый sync DevopsAction → DevopsAgentJob/HTTP-агент
# ============================================================================
# Когда пользователь жмёт кнопку и сразу закрывает вкладку, HTMX-поллинга больше нет,
# и локальная DevopsAction остаётся в running вечно (хотя удалённый job давно done).
# Эта задача периодически опрашивает источник истины и подтягивает результат.

def sync_action_once(action: DevopsAction) -> bool:
    """Опросить агента/локальный job и обновить DevopsAction.

    Возвращает True, если action в терминальном статусе (done/failed).
    Используется и из HTMX-вьюхи action_poll, и из фонового sync_action.
    Все сетевые ошибки глотает — пусть следующий тик попробует ещё раз.
    """
    if action.status in (DevopsAction.Status.DONE, DevopsAction.Status.FAILED):
        return True

    if not action.remote_job_id:
        action.status = DevopsAction.Status.FAILED
        action.output = "remote_job_id не установлен"
        action.finished_at = djtz.now()
        action.save(update_fields=["status", "output", "finished_at"])
        return True

    try:
        if action.action_type in LOCAL_ACTIONS:
            job_obj = DevopsAgentJob.objects.get(pk=action.remote_job_id)
            job = {"status": job_obj.status, "output": job_obj.output, "result": job_obj.result}
        else:
            job = AgentClient(action.environment).get_job(action.remote_job_id)
    except Exception:
        logger.warning("sync error for action %s", action.pk, exc_info=True)
        return False

    action.output = job.get("output") or ""
    if job.get("result"):
        action.params = {**(action.params or {}), "result": job["result"]}

    status = job.get("status")
    if status == "done":
        action.status = DevopsAction.Status.DONE
        action.finished_at = djtz.now()
        action.save(update_fields=["output", "params", "status", "finished_at"])
        return True
    if status == "failed":
        action.status = DevopsAction.Status.FAILED
        action.finished_at = djtz.now()
        action.save(update_fields=["output", "params", "status", "finished_at"])
        return True
    action.status = DevopsAction.Status.RUNNING
    action.save(update_fields=["output", "params", "status"])
    return False


# Шаг между опросами в фоновом sync (секунды). Согласован с HTMX-поллингом UI (2s),
# чуть реже — чтобы фон не дублировал live-апдейты, но успевал поймать done в течение 3 сек.
SYNC_TICK_SECONDS = 3

# Жёсткий потолок ожидания: 60 мин (часовой деплой/rebuild — реально, бесконечно — нет).
SYNC_MAX_ATTEMPTS = (60 * 60) // SYNC_TICK_SECONDS


@shared_task(name="devops.sync_action", queue="devops", bind=True, max_retries=None)
def sync_action(self, action_id: str, attempts: int = 0) -> None:
    """Фоновый «дожимающий» sync статуса DevopsAction.

    Каждый тик — один опрос источника истины через sync_action_once. Пока статус
    не терминальный, таск перепланирует сам себя через countdown=SYNC_TICK_SECONDS.
    Так одно worker-окно (devops-runner, concurrency=2) обслуживает любое количество
    одновременно «зависших» action — слот занят только на сам HTTP/DB опрос (<1s).
    """
    try:
        action = DevopsAction.objects.select_related("environment").get(pk=action_id)
    except DevopsAction.DoesNotExist:
        return
    except Exception:
        # Транзитивные ошибки БД (например, во время pull_db public schema на
        # секунды пропадает) — не считаем фатальными, просто переносим на тик дальше.
        logger.warning("sync_action transient DB error for %s — retry", action_id, exc_info=True)
        if attempts < SYNC_MAX_ATTEMPTS:
            sync_action.apply_async(args=[action_id, attempts + 1], countdown=SYNC_TICK_SECONDS)
        return

    try:
        done = sync_action_once(action)
    except Exception:
        logger.exception("sync_action crashed for %s", action_id)
        done = False

    if done:
        return

    if attempts >= SYNC_MAX_ATTEMPTS:
        action.refresh_from_db()
        if action.status == DevopsAction.Status.RUNNING:
            action.status = DevopsAction.Status.FAILED
            action.output = (action.output or "") + (
                f"\n\n[фоновый sync остановлен через {SYNC_MAX_ATTEMPTS * SYNC_TICK_SECONDS}s — "
                f"действие зависло на стороне агента?]"
            )
            action.finished_at = djtz.now()
            action.save(update_fields=["status", "output", "finished_at"])
        return

    sync_action.apply_async(args=[action_id, attempts + 1], countdown=SYNC_TICK_SECONDS)
