"""Dev-side UI: дашборд DevOps-панели."""
import logging
import traceback

from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .agent_client import AgentClient
from .models import DevopsAction, Environment

logger = logging.getLogger(__name__)


def _is_superuser(u):
    return u.is_authenticated and u.is_superuser


@user_passes_test(_is_superuser)
def dashboard(request):
    envs = Environment.objects.filter(is_active=True)
    actions = DevopsAction.objects.select_related("environment", "started_by")[:20]
    return render(request, "devops/dashboard.html", {"envs": envs, "actions": actions})


@user_passes_test(_is_superuser)
@require_POST
def run_action(request, env_id: int, action_type: str):
    """Создаёт DevopsAction и отправляет job на prod-агент."""
    env = get_object_or_404(Environment, pk=env_id, is_active=True)

    valid = {c[0] for c in DevopsAction.ActionType.choices}
    if action_type not in valid:
        return HttpResponseBadRequest("Unknown action type")

    action = DevopsAction.objects.create(
        environment=env,
        action_type=action_type,
        status=DevopsAction.Status.QUEUED,
        started_by=request.user,
    )

    try:
        client = AgentClient(env)
        job = client.create_job(action_type)
        action.remote_job_id = job["id"]
        action.status = DevopsAction.Status.RUNNING
        action.save(update_fields=["remote_job_id", "status"])
    except Exception as exc:
        action.status = DevopsAction.Status.FAILED
        action.output = f"Не удалось отправить job на агент:\n{traceback.format_exc()}"
        action.finished_at = timezone.now()
        action.save(update_fields=["status", "output", "finished_at"])
        logger.exception("Failed to enqueue action")

    return redirect("devops:action_detail", action_id=action.pk)


@user_passes_test(_is_superuser)
def action_detail(request, action_id):
    action = get_object_or_404(
        DevopsAction.objects.select_related("environment", "started_by"),
        pk=action_id,
    )
    return render(request, "devops/action_detail.html", {"action": action})


@user_passes_test(_is_superuser)
def action_poll(request, action_id):
    """HTMX polling: опрашивает prod-агент, обновляет action, рендерит partial."""
    action = get_object_or_404(
        DevopsAction.objects.select_related("environment"),
        pk=action_id,
    )

    if action.status in (DevopsAction.Status.DONE, DevopsAction.Status.FAILED):
        return render(request, "devops/partials/action_card.html", {"action": action})

    if not action.remote_job_id:
        action.status = DevopsAction.Status.FAILED
        action.output = "remote_job_id не установлен"
        action.finished_at = timezone.now()
        action.save(update_fields=["status", "output", "finished_at"])
        return render(request, "devops/partials/action_card.html", {"action": action})

    try:
        client = AgentClient(action.environment)
        job = client.get_job(action.remote_job_id)
    except Exception:
        # Сетевые ошибки не считаем фатальными — следующий polling попробует снова
        logger.warning("agent poll error for action %s", action.pk, exc_info=True)
        return render(request, "devops/partials/action_card.html", {"action": action})

    remote_status = job.get("status")
    action.output = job.get("output") or ""
    action.params = job.get("result") or {}  # храним result в params для простоты UI
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
