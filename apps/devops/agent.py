"""HTTP-агент: endpoints на проде, dev-панель ходит сюда с Bearer-токеном."""
import json
import os

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import DevopsAgentJob
from .tasks import HANDLERS, run_agent_job


def _check_token(request) -> bool:
    expected = os.environ.get("DEVOPS_AGENT_TOKEN", "")
    if not expected:
        return False
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Bearer "):
        return False
    return auth.removeprefix("Bearer ") == expected


def _unauthorized():
    return JsonResponse({"error": "unauthorized"}, status=401)


def _job_payload(job: DevopsAgentJob) -> dict:
    return {
        "id": str(job.pk),
        "action_type": job.action_type,
        "status": job.status,
        "output": job.output,
        "result": job.result,
        "params": job.params,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@csrf_exempt
@require_http_methods(["GET"])
def agent_ping(request):
    if not _check_token(request):
        return _unauthorized()
    return JsonResponse({
        "status": "ok",
        "service": "siricrm-devops-agent",
        "actions": sorted(HANDLERS.keys()),
    })


@csrf_exempt
@require_http_methods(["POST"])
def agent_jobs_create(request):
    """Создать job: тело запроса {action_type, params}."""
    if not _check_token(request):
        return _unauthorized()

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    action_type = data.get("action_type")
    if not action_type:
        return JsonResponse({"error": "action_type_required"}, status=400)
    if action_type not in HANDLERS:
        return JsonResponse(
            {"error": "unknown_action_type", "available": sorted(HANDLERS.keys())},
            status=400,
        )

    job = DevopsAgentJob.objects.create(
        action_type=action_type,
        params=data.get("params") or {},
        status=DevopsAgentJob.Status.QUEUED,
    )
    run_agent_job.delay(str(job.pk))
    return JsonResponse(_job_payload(job), status=201)


@csrf_exempt
@require_http_methods(["GET"])
def agent_jobs_detail(request, job_id):
    if not _check_token(request):
        return _unauthorized()
    try:
        job = DevopsAgentJob.objects.get(pk=job_id)
    except DevopsAgentJob.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse(_job_payload(job))
