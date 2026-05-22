"""UI импорта из Bubble.io — панель аудита и переноса данных.

Доступ — только суперпользователь. Вкладки по сущностям:
Man → Client, ProjectBFL → Service, Money → Payment/Charge.
MessageWSP и Files — на следующем этапе (нужен фоновый скачиватель).

Порядок Apply важен из-за зависимостей: сначала клиенты, затем услуги,
затем платежи (applier сам вернёт ошибку, если зависимость не готова).
"""
import logging

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.permissions import require_superuser

from . import bubble_api
from .extractors import extract_display
from .models import BubbleRecord, BubbleFetchState
from .services import fetch_batch, DEFAULT_BATCH, get_state
from .appliers import apply_approved

logger = logging.getLogger("bubble_import")

PAGE_SIZE = 50

# Активные вкладки. Порядок = рекомендуемый порядок Apply (зависимости).
ACTIVE_ENTITIES = ["Man", "ProjectBFL", "Money", "MessageWSP", "Files"]
ENTITY_LABELS = {
    "Man": "Клиенты",
    "ProjectBFL": "Услуги",
    "Money": "Платежи",
    "MessageWSP": "WhatsApp",
    "Files": "Файлы",
}
# Поля, доступные для inline-правки — только у клиентов.
EDITABLE_FIELDS = {
    "Man": {"fName", "lName", "mName", "tel", "email"},
}


def _check_entity(entity: str):
    if entity not in ACTIVE_ENTITIES:
        raise Http404(f"Сущность {entity} недоступна")


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


def _filtered_records(request, entity: str):
    """QuerySet записей сущности с учётом активного фильтра и поиска."""
    flt = request.GET.get("filter") or request.POST.get("filter") or "all"
    qs = BubbleRecord.objects.filter(entity=entity)
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


def _tabs(current: str) -> list:
    """Вкладки со счётчиками выгружено/всего по каждой сущности."""
    states = {s.entity: s for s in BubbleFetchState.objects.all()}
    tabs = []
    for ent in ACTIVE_ENTITIES:
        qs = BubbleRecord.objects.filter(entity=ent)
        st = states.get(ent)
        tabs.append({
            "entity": ent,
            "label": ENTITY_LABELS[ent],
            "fetched": qs.count(),
            "imported": qs.filter(status="imported").count(),
            "remote": st.total_remote if st else 0,
            "active": ent == current,
        })
    return tabs


def _entity_context(request, entity: str) -> dict:
    qs, flt, search = _filtered_records(request, entity)
    qs = qs.order_by("bubble_created", "id")
    paginator = Paginator(qs, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page") or 1)
    return {
        "entity": entity,
        "entity_label": ENTITY_LABELS[entity],
        "active_entities": ACTIVE_ENTITIES,
        "entity_labels": ENTITY_LABELS,
        "tabs": _tabs(entity),
        "page_obj": page,
        "filter": flt,
        "q": search,
        "stats": _stats(entity),
        "batch": DEFAULT_BATCH,
        "api_ok": bubble_api.is_configured(),
        "editable": entity in EDITABLE_FIELDS,
    }


@login_required
@require_superuser
def panel(request, entity="Man"):
    """Главная страница импорта (грузится в #content-area)."""
    _check_entity(entity)
    return render(request, "bubble_import/panel.html", _entity_context(request, entity))


@login_required
@require_superuser
def entity_table(request, entity):
    """HTMX-партиал таблицы сущности."""
    _check_entity(entity)
    return render(request, "bubble_import/partials/entity_table.html",
                  _entity_context(request, entity))


@login_required
@require_superuser
@require_POST
def fetch(request, entity):
    """Выкачать следующую порцию записей из Bubble."""
    _check_entity(entity)
    if not bubble_api.is_configured():
        return HttpResponseBadRequest("Bubble API не настроен (BUBBLE_API_TOKEN)")
    try:
        result = fetch_batch(entity, batch=DEFAULT_BATCH)
        logger.info("Bubble fetch %s: %s", entity, result)
    except bubble_api.BubbleAPIError as e:
        return HttpResponse(f"Ошибка Bubble API: {e}", status=502)
    return render(request, "bubble_import/partials/entity_table.html",
                  _entity_context(request, entity))


@login_required
@require_superuser
@require_POST
def toggle_approve(request, entity, pk):
    """Переключить флаг одобрения одной записи."""
    _check_entity(entity)
    rec = BubbleRecord.objects.filter(pk=pk, entity=entity).first()
    if not rec:
        return HttpResponse(status=404)
    rec.approved = not rec.approved
    rec.save(update_fields=["approved"])
    return render(request, "bubble_import/partials/row.html",
                  {"rec": rec, "entity": entity, "editable": entity in EDITABLE_FIELDS})


@login_required
@require_superuser
@require_POST
def bulk_approve(request, entity):
    """Одобрить/снять все записи текущей страницы (по номеру страницы)."""
    _check_entity(entity)
    action = request.POST.get("action")
    qs, _flt, _q = _filtered_records(request, entity)
    qs = qs.order_by("bubble_created", "id")
    page = Paginator(qs, PAGE_SIZE).get_page(request.POST.get("page") or 1)
    page_ids = [r.pk for r in page.object_list]
    BubbleRecord.objects.filter(pk__in=page_ids).exclude(status="imported").update(
        approved=(action == "approve"),
    )
    return render(request, "bubble_import/partials/entity_table.html",
                  _entity_context(request, entity))


@login_required
@require_superuser
@require_POST
def select_all(request, entity):
    """Одобрить/снять ВСЕ записи (по активному фильтру), кроме импортированных."""
    _check_entity(entity)
    action = request.POST.get("action")
    qs, _flt, _q = _filtered_records(request, entity)
    qs.exclude(status="imported").update(approved=(action == "approve"))
    return render(request, "bubble_import/partials/entity_table.html",
                  _entity_context(request, entity))


@login_required
@require_superuser
@require_POST
def edit_field(request, entity, pk):
    """Inline-правка одного поля — сохраняется в overrides записи."""
    _check_entity(entity)
    if entity not in EDITABLE_FIELDS:
        return HttpResponseBadRequest("Правка недоступна для этой сущности")
    rec = BubbleRecord.objects.filter(pk=pk, entity=entity).first()
    if not rec:
        return HttpResponse(status=404)
    field = request.POST.get("field")
    if field not in EDITABLE_FIELDS[entity]:
        return HttpResponseBadRequest("Поле нельзя редактировать")
    value = (request.POST.get("value") or "").strip()

    overrides = dict(rec.overrides or {})
    if value:
        overrides[field] = value
    else:
        overrides.pop(field, None)
    rec.overrides = overrides
    merged = {**(rec.raw or {}), **overrides}
    for k, v in extract_display(entity, merged).items():
        setattr(rec, k, v)
    rec.save(update_fields=["overrides", "display_title", "display_subtitle"])
    return render(request, "bubble_import/partials/row.html",
                  {"rec": rec, "entity": entity, "editable": True})


@login_required
@require_superuser
@require_POST
def apply(request, entity):
    """Импортировать одобренные записи сущности в модели SiriCRM."""
    _check_entity(entity)
    result = apply_approved(entity)
    logger.info("Bubble apply %s: %s", entity, result)
    ctx = _entity_context(request, entity)
    ctx["apply_result"] = result
    return render(request, "bubble_import/partials/entity_table.html", ctx)
