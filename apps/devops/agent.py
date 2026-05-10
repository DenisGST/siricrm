"""HTTP-агент: endpoints, которые слушаются на проде, вызываются с dev.

Аутентификация: Bearer-токен из env-переменной DEVOPS_AGENT_TOKEN.
Реальная логика добавляется в этапах 3.2+.
"""
import os

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods


def _check_token(request) -> bool:
    expected = os.environ.get("DEVOPS_AGENT_TOKEN", "")
    if not expected:
        return False  # без токена ничего не пускаем
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Bearer "):
        return False
    return auth.removeprefix("Bearer ") == expected


def _unauthorized():
    return JsonResponse({"error": "unauthorized"}, status=401)


@csrf_exempt
@require_http_methods(["GET"])
def agent_ping(request):
    """Пинг агента — проверка токена и доступности."""
    if not _check_token(request):
        return _unauthorized()
    return JsonResponse({"status": "ok", "service": "siricrm-devops-agent"})
