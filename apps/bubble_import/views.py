"""UI импорта из Bubble.io — панель аудита и переноса данных.

Доступ — только суперпользователь. На этапе B2/B3 реализована вкладка
«Клиенты» (Man → Client): постраничный просмотр staging, чекбоксы
одобрения, inline-правка ключевых полей, запуск Apply.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.core.permissions import require_superuser

from . import bubble_api
from .extractors import extract_display
from .models import BubbleRecord, BubbleFetchState
from .services import fetch_batch, DEFAULT_BATCH, get_state
from .appliers import apply_approved

logger = logging.getLogger("bubble_import")

PAGE_SIZE = 50
ENTITY = "Man"  # на этапе B2/B3 — единственная активная вкладка

# Поля Man, которые можно править inline перед Apply.
EDITABLE_FIELDS = {"fName", "lName", "mName", "tel", "email"}


def _stats(entity: str) -> dict:
    qs = BubbleRecord.objects.filter(entity=entity)
    state = get_state(entity)
    total_fetched = qs.count()
    imported = qs.filter(status="imported").count()
    return {
        "total_remote": state.total_remote,
        "total_fetched": total_fetched,
        "approved": qs.filter(approved=True).count(),
        "imported": imported,
        "errors": qs.filter(status="error").count(),
        "pending": qs.filter(status="pending").count(),
        "not_imported": total_fetched - imported,
        "last_fetch_at": state.last_fetch_at,
    }


def _filtered_records(request):
    """QuerySet записей Man с учётом активного фильтра и поиска."""
    flt = request.GET.get("filter") or request.POST.get("filter") or "all"
    qs = BubbleRecord.objects.filter(entity=ENTITY)
    if flt == "pending":
        qs = qs.filter(status="pending")
    elif flt == "approved":
        qs = qs.filter(approved=True)
    elif flt == "imported":
        qs = qs.filter(status="imported")
    elif flt == "error":
        qs = qs.filter(status="error")

    search = (request.GET.get("q") or request.POST.get("q") or "").strip()
    if search:
        qs = qs.filter(display_title__icontains=search)
    return qs, flt, search


def _clients_context(request):
    """Контекст таблицы клиентов: фильтр + пагинация."""
    qs, flt, search = _filtered_records(request)
    qs = qs.order_by("bubble_created", "id")
    paginator = Paginator(qs, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page") or 1)

    return {
        "page_obj": page,
        "filter": flt,
        "q": search,
        "stats": _stats(ENTITY),
        "batch": DEFAULT_BATCH,
        "api_ok": bubble_api.is_configured(),
    }


@login_required
@require_superuser
def panel(request):
    """Главная страница импорта (грузится в #content-area)."""
    return render(request, "bubble_import/panel.html", _clients_context(request))


@login_required
@require_superuser
def clients_table(request):
    """HTMX-партиал таблицы клиентов."""
    return render(request, "bubble_import/partials/clients_table.html", _clients_context(request))


@login_required
@require_superuser
@require_POST
def fetch(request):
    """Выкачать следующую порцию записей Man из Bubble."""
    if not bubble_api.is_configured():
        return HttpResponseBadRequest("Bubble API не настроен (BUBBLE_API_TOKEN)")
    try:
        result = fetch_batch(ENTITY, batch=DEFAULT_BATCH)
        logger.info("Bubble fetch result: %s", result)
    except bubble_api.BubbleAPIError as e:
        return HttpResponse(f"Ошибка Bubble API: {e}", status=502)
    return render(request, "bubble_import/partials/clients_table.html", _clients_context(request))


@login_required
@require_superuser
@require_POST
def toggle_approve(request, pk):
    """Переключить флаг одобрения одной записи."""
    rec = BubbleRecord.objects.filter(pk=pk, entity=ENTITY).first()
    if not rec:
        return HttpResponse(status=404)
    rec.approved = not rec.approved
    rec.save(update_fields=["approved"])
    return render(request, "bubble_import/partials/row.html", {"rec": rec})


@login_required
@require_superuser
@require_POST
def bulk_approve(request):
    """Одобрить/снять все записи на текущей странице (id списком)."""
    ids = request.POST.getlist("ids")
    action = request.POST.get("action")  # approve | unapprove
    if ids:
        BubbleRecord.objects.filter(pk__in=ids, entity=ENTITY).update(
            approved=(action == "approve"),
        )
    return render(request, "bubble_import/partials/clients_table.html", _clients_context(request))


@login_required
@require_superuser
@require_POST
def select_all(request):
    """Одобрить/снять ВСЕ записи (по активному фильтру), не только страницу.

    Уже импортированные не трогаем.
    """
    action = request.POST.get("action")  # approve | unapprove
    qs, _flt, _q = _filtered_records(request)
    qs.exclude(status="imported").update(approved=(action == "approve"))
    return render(request, "bubble_import/partials/clients_table.html", _clients_context(request))


@login_required
@require_superuser
@require_POST
def edit_field(request, pk):
    """Inline-правка одного поля — сохраняется в overrides записи."""
    rec = BubbleRecord.objects.filter(pk=pk, entity=ENTITY).first()
    if not rec:
        return HttpResponse(status=404)
    field = request.POST.get("field")
    if field not in EDITABLE_FIELDS:
        return HttpResponseBadRequest("Поле нельзя редактировать")
    value = (request.POST.get("value") or "").strip()

    overrides = dict(rec.overrides or {})
    if value:
        overrides[field] = value
    else:
        overrides.pop(field, None)
    rec.overrides = overrides
    # пересчитать display-поля с учётом правок
    merged = {**(rec.raw or {}), **overrides}
    for k, v in extract_display(ENTITY, merged).items():
        setattr(rec, k, v)
    rec.save(update_fields=["overrides", "display_title", "display_subtitle"])
    return render(request, "bubble_import/partials/row.html", {"rec": rec})


@login_required
@require_superuser
@require_POST
def apply(request):
    """Импортировать одобренные записи Man → Client."""
    result = apply_approved(ENTITY)
    logger.info("Bubble apply Man: %s", result)
    ctx = _clients_context(request)
    ctx["apply_result"] = result
    return render(request, "bubble_import/partials/clients_table.html", ctx)
