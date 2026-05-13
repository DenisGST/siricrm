"""Dev-side UI: дашборд DevOps-панели (только суперюзер, только на dev-сервере)."""
import logging
import traceback

from django.apps import apps
from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .agent_client import AgentClient, AgentError
from .models import DevopsAction, DevopsAgentJob, Environment
from .tasks import LOCAL_ACTIONS, run_agent_job, sync_action, sync_action_once

logger = logging.getLogger(__name__)

# Действия, которые меняют состояние и требуют подтверждения в UI.
DANGEROUS_ACTIONS = {"pull_db", "push_db", "deploy", "rebuild", "rollback",
                     "pull_tables", "push_tables"}

# Какие POST-параметры могут приходить списком (multi-select). Остальные — скалярные.
MULTI_VALUE_PARAMS = {"models"}

# Apps которые в UI скрываем из выбора (служебные Django/3rd-party).
_HIDDEN_APPS = {
    "admin", "auth", "contenttypes", "sessions", "sites",
    "django_celery_beat", "django_celery_results",
}

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


def _list_user_models() -> list[dict]:
    """Все «пользовательские» модели для UI выборочного sync таблиц.

    Возвращает плоский список dicts: {app_label, model_name, label, verbose_name, count}.
    Сгруппировать по app_label шаблон уже сам.
    Прокси и абстрактные пропускаем (loaddata не умеет).
    """
    result: list[dict] = []
    for model in apps.get_models():
        if model._meta.abstract or model._meta.proxy:
            continue
        app_label = model._meta.app_label
        if app_label in _HIDDEN_APPS:
            continue
        full = f"{app_label}.{model._meta.model_name}"
        try:
            count = model.objects.count()
        except Exception:
            count = None
        result.append({
            "app_label": app_label,
            "model_name": model._meta.model_name,
            "label": full,
            "verbose_name": str(model._meta.verbose_name_plural or model._meta.verbose_name or model.__name__),
            "count": count,
        })
    result.sort(key=lambda m: (m["app_label"], m["model_name"]))
    return result


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
    user_models = _list_user_models()
    # Группировка по app_label для шаблона.
    models_by_app: dict[str, list[dict]] = {}
    for m in user_models:
        models_by_app.setdefault(m["app_label"], []).append(m)
    return render(request, "devops/dashboard.html", {
        "envs": envs,
        "dev_env": dev_env,
        "prod_env": prod_env,
        "actions": actions,
        "readonly_actions": READONLY_ACTIONS,
        "models_by_app": models_by_app,
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

    # Доп. параметры из формы (например target_commit для rollback, branch для deploy,
    # список моделей для pull_tables/push_tables).
    form_params: dict = {}
    for k in request.POST.keys():
        if k in ("csrfmiddlewaretoken", "confirm"):
            continue
        if k in MULTI_VALUE_PARAMS:
            vals = [v for v in request.POST.getlist(k) if v]
            if vals:
                form_params[k] = vals
        else:
            v = request.POST.get(k)
            if v not in ("", None):
                form_params[k] = v

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
        # Фоновый дожим статуса — чтобы action закрылся даже если пользователь закрыл вкладку.
        # Не критично: если broker недоступен, HTMX-поллинг всё равно подтянет статус,
        # а action уже в RUNNING с валидным remote_job_id.
        try:
            sync_action.delay(str(action.pk))
        except Exception:
            logger.warning("sync_action enqueue failed for %s — fallback to HTMX poll", action.pk, exc_info=True)
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
    """HTMX polling: подтягивает свежий статус через общий sync-helper.

    Фоновый Celery sync_action делает то же самое и без UI — здесь идентичный код
    нужен для мгновенного апдейта при открытой вкладке (живой лог раз в 2с).

    Если пришли сюда напрямую (не HTMX) — редиректим на action_detail. Иначе
    пользователь увидит «голый» HTML-фрагмент (так бывает, например, когда
    сессия слетела во время pull_db и в next= остался URL поллинга).
    """
    if "HX-Request" not in request.headers:
        return redirect("devops:action_detail", action_id=action_id)
    action = get_object_or_404(
        DevopsAction.objects.select_related("environment"),
        pk=action_id,
    )
    sync_action_once(action)
    return render(request, "devops/partials/action_card.html",
                  {"action": action, "readonly_render_types": RICH_READONLY})
