"""Dev-side UI: дашборд DevOps-панели (только суперюзер, только на dev-сервере)."""
import logging
import traceback

from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .agent_client import AgentClient, AgentError
from .models import DevopsAction, DevopsAgentJob, Environment
from .tasks import run_agent_job

logger = logging.getLogger(__name__)

# Action types, выполняемые ЛОКАЛЬНО на dev-сервере (в devops-runner этого сервера),
# а не через агента целевого окружения. Значение — функция env → стартовые params.
LOCAL_ACTIONS = {
    # затянуть БД источника СЮДА (на dev): источник = окружение, на котором нажали
    "pull_db": lambda env: {"source_env_id": env.id},
    # залить ЭТУ БД (dev) в целевое окружение: цель = окружение, на котором нажали
    "push_db": lambda env: {"target_env_id": env.id},
}

# Действия, которые меняют состояние и требуют подтверждения в UI.
DANGEROUS_ACTIONS = {"pull_db", "push_db", "deploy", "rebuild", "rollback"}

# Действия только-для-чтения — не считаем их «важными» в истории по умолчанию.
READONLY_ACTIONS = {"status", "list_backups", "s3_stats", "git_log"}

# Действия, у которых структурированный рендер результата — главное (а текстовый лог
# вторичен), и которые безопасно перезапустить одной кнопкой.
RICH_READONLY = {"status", "list_backups", "s3_stats", "git_log"}


def _is_superuser(u):
    return u.is_authenticated and u.is_superuser


def _latest_action(env, action_type):
    return (
        DevopsAction.objects.filter(environment=env, action_type=action_type)
        .order_by("-started_at")
        .first()
    )


@user_passes_test(_is_superuser)
def dashboard(request):
    envs = list(Environment.objects.filter(is_active=True))
    for env in envs:
        env.label = env.name.upper()
        env.latest_status = _latest_action(env, "status")
        env.latest_s3 = _latest_action(env, "s3_stats")
    # Удобные ссылки на «эталонные» окружения для секций UI.
    by_name = {e.name: e for e in envs}
    dev_env = by_name.get("dev")
    prod_env = by_name.get("prod")

    actions = list(
        DevopsAction.objects.select_related("environment", "started_by")[:25]
    )
    return render(request, "devops/dashboard.html", {
        "envs": envs,
        "dev_env": dev_env,
        "prod_env": prod_env,
        "actions": actions,
        "readonly_actions": READONLY_ACTIONS,
    })


@user_passes_test(_is_superuser)
def history_partial(request):
    """HTMX: таблица истории действий (для авто-обновления на дашборде)."""
    actions = list(DevopsAction.objects.select_related("environment", "started_by")[:25])
    return render(request, "devops/partials/_history_table.html", {
        "actions": actions, "readonly_actions": READONLY_ACTIONS,
    })


@user_passes_test(_is_superuser)
@require_POST
def run_action(request, env_id: int, action_type: str):
    """Создаёт DevopsAction и запускает job: локально (devops-runner) или через агента."""
    env = get_object_or_404(Environment, pk=env_id, is_active=True)

    valid = {c[0] for c in DevopsAction.ActionType.choices}
    if action_type not in valid:
        return HttpResponseBadRequest("Unknown action type")

    # Доп. параметры из формы (например target_commit для rollback, branch для deploy).
    form_params = {
        k: v for k, v in request.POST.items()
        if k not in ("csrfmiddlewaretoken", "confirm") and v not in ("", None)
    }

    action = DevopsAction.objects.create(
        environment=env,
        action_type=action_type,
        status=DevopsAction.Status.QUEUED,
        started_by=request.user,
    )

    try:
        if action_type in LOCAL_ACTIONS:
            params = LOCAL_ACTIONS[action_type](env)
            params.update(form_params)
            job = DevopsAgentJob.objects.create(
                action_type=action_type,
                params=params,
                status=DevopsAgentJob.Status.QUEUED,
            )
            run_agent_job.delay(str(job.pk))
            action.remote_job_id = str(job.pk)
            action.params = params
        else:
            client = AgentClient(env)
            job = client.create_job(action_type, params=form_params or None)
            action.remote_job_id = job["id"]
            action.params = form_params
        action.status = DevopsAction.Status.RUNNING
        action.save(update_fields=["remote_job_id", "params", "status"])
    except (AgentError, RuntimeError, ValueError) as e:
        # Ожидаемые ошибки (агент недоступен/устарел, нет токена и т.п.) — показываем коротко.
        action.status = DevopsAction.Status.FAILED
        action.output = f"Не удалось запустить:\n{e}"
        action.finished_at = timezone.now()
        action.save(update_fields=["status", "output", "finished_at"])
        logger.warning("enqueue %s/%s failed: %s", env.name, action_type, e)
    except Exception:
        action.status = DevopsAction.Status.FAILED
        action.output = f"Не удалось запустить (внутренняя ошибка):\n{traceback.format_exc()}"
        action.finished_at = timezone.now()
        action.save(update_fields=["status", "output", "finished_at"])
        logger.exception("Failed to enqueue action %s/%s", env.name, action_type)

    return redirect("devops:action_detail", action_id=action.pk)


@user_passes_test(_is_superuser)
def action_detail(request, action_id):
    action = get_object_or_404(
        DevopsAction.objects.select_related("environment", "started_by"),
        pk=action_id,
    )
    return render(request, "devops/action_detail.html", {
        "action": action, "readonly_render_types": RICH_READONLY,
    })


@user_passes_test(_is_superuser)
def action_poll(request, action_id):
    """HTMX polling: опрашивает локальный DevopsAgentJob либо агента целевого окружения."""
    action = get_object_or_404(
        DevopsAction.objects.select_related("environment"),
        pk=action_id,
    )

    if action.status in (DevopsAction.Status.DONE, DevopsAction.Status.FAILED):
        return render(request, "devops/partials/action_card.html",
                      {"action": action, "readonly_render_types": RICH_READONLY})

    if not action.remote_job_id:
        action.status = DevopsAction.Status.FAILED
        action.output = "remote_job_id не установлен"
        action.finished_at = timezone.now()
        action.save(update_fields=["status", "output", "finished_at"])
        return render(request, "devops/partials/action_card.html",
                      {"action": action, "readonly_render_types": RICH_READONLY})

    try:
        if action.action_type in LOCAL_ACTIONS:
            job_obj = DevopsAgentJob.objects.get(pk=action.remote_job_id)
            job = {"status": job_obj.status, "output": job_obj.output, "result": job_obj.result}
        else:
            client = AgentClient(action.environment)
            job = client.get_job(action.remote_job_id)
    except Exception:
        logger.warning("poll error for action %s", action.pk, exc_info=True)
        return render(request, "devops/partials/action_card.html",
                      {"action": action, "readonly_render_types": RICH_READONLY})

    remote_status = job.get("status")
    action.output = job.get("output") or ""
    if job.get("result"):
        action.params = {**(action.params or {}), "result": job["result"]}
    if remote_status == "done":
        action.status = DevopsAction.Status.DONE
        action.finished_at = timezone.now()
        action.save(update_fields=["output", "params", "status", "finished_at"])
    elif remote_status == "failed":
        action.status = DevopsAction.Status.FAILED
        action.finished_at = timezone.now()
        action.save(update_fields=["output", "params", "status", "finished_at"])
    else:
        action.status = DevopsAction.Status.RUNNING
        action.save(update_fields=["output", "params", "status"])

    return render(request, "devops/partials/action_card.html", {"action": action})
