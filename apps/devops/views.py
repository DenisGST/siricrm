"""Заглушки для этапа 3.1. Реальная логика добавляется в этапах 3.3+."""
from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.shortcuts import render

from .models import DevopsAction, Environment


def _is_superuser(u):
    return u.is_authenticated and u.is_superuser


@user_passes_test(_is_superuser)
def dashboard(request):
    """Дашборд DevOps-панели (этап 3.4 наполнит)."""
    envs = Environment.objects.filter(is_active=True)
    actions = DevopsAction.objects.select_related("environment", "started_by")[:20]
    return render(request, "devops/dashboard.html", {"envs": envs, "actions": actions})
