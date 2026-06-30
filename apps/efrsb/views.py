"""Вьюхи интеграции ЕФРСБ.

Вкладка «Публикации» карточки дела (apps.procedure) → под-вкладки ЕФРСБ / КоммерсантЪ.
ЕФРСБ: генерация текста сообщений (движок АФД), реестр публикаций, (A2/A3) поиск
должника и мониторинг. КоммерсантЪ — заглушка (другая ветка).

Гейт/гард услуги БФЛ переиспользуем из apps.procedure (require_procedures, _bfl_service).
"""
from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.core.permissions import is_references_access
from apps.procedure import services as proc_services
from apps.procedure.permissions import require_procedures
from apps.procedure.views import _NotBFL, _bfl_service

from .models import EfrsbMessageType, EfrsbPublication

log = logging.getLogger(__name__)

import json
import os

_CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "reference_data", "categories.json")


def _load_categories():
    """Официальная группировка типов ЕФРСБ (дерево ЛК). [{title, codes:[...]}]."""
    try:
        with open(_CATEGORIES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("efrsb: не прочитать categories.json")
        return []


def _build_type_tree(types):
    """Сгруппировать активные типы по официальному дереву; остальное → «Прочее».

    Возвращает список групп для JSON: [{title, items:[{id,name,bfl,draft}]}].
    title=None — типы верхнего уровня (без группы).
    """
    by_code = {t.code: t for t in types}
    used = set()
    groups = []
    for cat in _load_categories():
        items = []
        for code in cat.get("codes", []):
            t = by_code.get(code)
            if t is not None:
                items.append(t)
                used.add(code)
        if items:
            groups.append({"title": cat.get("title"), "items": items})
    rest = [t for t in types if t.code not in used]
    if rest:
        groups.append({"title": "Прочее", "items": rest})

    def _leaf(t):
        return {"id": str(t.id), "name": t.name, "bfl": t.is_bfl, "draft": t.is_draft}

    return [{"title": g["title"], "items": [_leaf(t) for t in g["items"]]} for g in groups]


def _actor(request):
    return getattr(request.user, "employee", None)


def _case(request, service_id):
    """(service, case) с гардом БФЛ; бросает _NotBFL → 403 в вызывающей вьюхе."""
    service = _bfl_service(request, service_id)
    return service, proc_services.ensure_case(service)


def _efrsb_trigger():
    """Пустой ответ + сигнал перезагрузить реестр публикаций ЕФРСБ."""
    return HttpResponse(headers={"HX-Trigger": "reloadEfrsb"})


# ── Вкладка «Публикации» (контейнер под-вкладок) ────────────────────────────

@never_cache
@login_required
@require_procedures
def tab_publications(request, service_id):
    try:
        service, _ = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    return render(request, "efrsb/_tab_publications.html", {"service": service})


@never_cache
@login_required
@require_procedures
def subtab_kommersant(request, service_id):
    try:
        service, _ = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    return render(request, "efrsb/_subtab_kommersant.html", {"service": service})


# ── Под-вкладка ЕФРСБ ───────────────────────────────────────────────────────

def _efrsb_context(service, case) -> dict:
    proc = case.current_procedure
    # Типы для дропдауна «Сформировать текст» — фильтр по виду текущей процедуры.
    types = EfrsbMessageType.objects.filter(is_active=True).order_by("order", "name")
    if proc is not None:
        types = [t for t in types if t.applies_to_kind(proc.kind)]
    else:
        types = list(types)
    publications = list(
        case.efrsb_publications.select_related("message_type", "procedure",
                                                "content_pdf", "content_docx")
        .prefetch_related("files__stored_file")
        .all()
    )
    link = getattr(case, "efrsb_link", None)
    from . import config as efrsb_config
    # Дерево типов (официальная группировка ЛК ЕФРСБ) для селектора.
    tree_json = _build_type_tree(types)
    return {
        "service": service,
        "case": case,
        "current_procedure": proc,
        "message_types": types,
        "tree_json": tree_json,
        "bfl_count": sum(1 for t in types if t.is_bfl),
        "publications": publications,
        "link": link,
        "efrsb_monitor_enabled": efrsb_config.is_configured(),
    }


@never_cache
@login_required
@require_procedures
def subtab_efrsb(request, service_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    return render(request, "efrsb/_subtab_efrsb.html", _efrsb_context(service, case))


@login_required
@require_procedures
@require_POST
def publication_add(request, service_id):
    """Создать заготовку публикации (origin=internal, draft) выбранного типа."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    mt = EfrsbMessageType.objects.filter(pk=(request.POST.get("message_type") or None)).first()
    if mt is None:
        return _toast("Выберите тип сообщения.", "warning")
    EfrsbPublication.objects.create(
        case=case,
        procedure=case.current_procedure,
        message_type=mt,
        kind=(EfrsbPublication.KIND_REPORT if mt.api_kind == EfrsbMessageType.API_KIND_REPORT
              else EfrsbPublication.KIND_MESSAGE),
        origin=EfrsbPublication.ORIGIN_INTERNAL,
        status=EfrsbPublication.STATUS_DRAFT,
        title=mt.name,
    )
    return _efrsb_trigger()


@never_cache
@login_required
@require_procedures
def publication_generate_form(request, service_id, pub_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(EfrsbPublication, pk=pub_id, case=case)
    from .generator import check_publication_data
    all_ok, groups = check_publication_data(pub, overrides=pub.overrides)
    return render(request, "efrsb/_publication_generate_modal.html", {
        "service": service, "pub": pub,
        "check_all_ok": all_ok, "check_groups": groups,
        "body_value": (pub.overrides or {}).get("Текст сообщения", ""),
    })


@login_required
@require_procedures
@require_POST
def publication_generate(request, service_id, pub_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(EfrsbPublication, pk=pub_id, case=case)
    from .generator import EfrsbGenError, generate_publication
    overrides = dict(pub.overrides or {})
    overrides["Текст сообщения"] = (request.POST.get("body") or "").strip()
    try:
        generate_publication(pub, overrides=overrides, employee=_actor(request))
    except EfrsbGenError as exc:
        from .generator import check_publication_data
        all_ok, groups = check_publication_data(pub, overrides=overrides)
        return render(request, "efrsb/_publication_generate_modal.html", {
            "service": service, "pub": pub, "error": str(exc),
            "check_all_ok": all_ok, "check_groups": groups,
            "body_value": overrides["Текст сообщения"],
        })
    except Exception:
        log.exception("publication_generate failed")
        return render(request, "efrsb/_publication_generate_modal.html", {
            "service": service, "pub": pub,
            "error": "Не удалось сформировать текст (ошибка конвертации). Попробуйте ещё раз.",
            "body_value": overrides["Текст сообщения"],
        })
    return _efrsb_trigger()


@login_required
@require_procedures
@require_POST
def publication_delete(request, service_id, pub_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(EfrsbPublication, pk=pub_id, case=case)
    # Обнаруженные в реестре не удаляем вручную (только наши заготовки).
    if pub.origin == EfrsbPublication.ORIGIN_INTERNAL:
        pub.delete()
    return _efrsb_trigger()


# ── Поиск должника / мониторинг (A2/A3) ─────────────────────────────────────

def _toast(msg: str, kind: str = "info"):
    """204 + HX-Trigger reloadEfrsb + тост (showToast слушает событие efrsbToast)."""
    import json
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = json.dumps({"reloadEfrsb": True, "efrsbToast": {"msg": msg, "kind": kind}})
    return resp


@login_required
@require_procedures
@require_POST
def resolve_bankrupt(request, service_id):
    """Найти должника в ЕФРСБ по ИНН/СНИЛС (синхронно — 1 запрос)."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    from . import config, services
    if not config.is_configured():
        return _toast("ЕФРСБ не настроен (нет доступа к API).", "error")
    link = services.resolve_bankrupt_guid(case, force=True)
    if link.bankrupt_guid:
        return _toast("Должник найден в ЕФРСБ.", "success")
    if link.candidates:
        return _toast(f"Найдено несколько кандидатов ({len(link.candidates)}) — выберите.", "warning")
    return _toast(link.last_error or "Должник в ЕФРСБ не найден.", "warning")


@login_required
@require_procedures
@require_POST
def confirm_bankrupt(request, service_id):
    """Подтвердить выбранного должника из кандидатов."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    from . import services
    guid = (request.POST.get("guid") or "").strip()
    if not guid:
        return _toast("Не передан GUID должника.", "error")
    services.confirm_bankrupt(case, guid)
    return _toast("Должник подтверждён.", "success")


@login_required
@require_procedures
@require_POST
def refresh_now(request, service_id):
    """Обновить публикации из ЕФРСБ (синхронно — окно 31 день, объём мал)."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    from . import config, services
    from .client import EfrsbError
    if not config.is_configured():
        return _toast("ЕФРСБ не настроен (нет доступа к API).", "error")
    try:
        link = services.resolve_bankrupt_guid(case)
        if not link.bankrupt_guid:
            return _toast("Должник в ЕФРСБ не привязан — сначала найдите/подтвердите.", "warning")
        stats = services.sync_case(case, download_files=config.download_files_default())
    except EfrsbError as exc:
        return _toast(f"Ошибка ЕФРСБ: {exc}", "error")
    new = stats.get("new", 0)
    return _toast(f"Обновлено из ЕФРСБ. Новых публикаций: {new}.", "success")


# ── Справочник «Типы сообщений ЕФРСБ» (Справочники) ─────────────────────────

@user_passes_test(is_references_access)
def references_message_types(request):
    items = (EfrsbMessageType.objects.select_related("template", "isk_template")
             .order_by("order", "name"))
    return render(request, "efrsb/references_message_types.html", {"items": items})


@user_passes_test(is_references_access)
def reference_message_type_edit(request, pk=None):
    from .forms import EfrsbMessageTypeForm
    obj = get_object_or_404(EfrsbMessageType, pk=pk) if pk else None
    if request.method == "POST":
        form = EfrsbMessageTypeForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadEfrsbTypes"})
    else:
        form = EfrsbMessageTypeForm(instance=obj)
    from apps.afd.models import DocumentTemplate, IskTemplate
    return render(request, "efrsb/message_type_form_modal.html", {
        "form": form, "obj": obj,
        "doc_templates": DocumentTemplate.objects.filter(
            kind=DocumentTemplate.KIND_EFRSB, is_active=True).order_by("name"),
        "isk_templates": IskTemplate.objects.filter(is_active=True).order_by("name"),
    })


@user_passes_test(is_references_access)
@require_POST
def reference_message_type_delete(request, pk):
    get_object_or_404(EfrsbMessageType, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadEfrsbTypes"})
