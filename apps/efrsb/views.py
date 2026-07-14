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

    Тип с подтипами (напр. «Сообщение о судебном акте» → 24 типа судебных актов)
    отдаётся как узел с `subs` — в UI раскрывается, выбирается подтип.
    Фильтрация по БФЛ/виду процедуры — на клиенте (чтобы «Показать все» её обходил).

    [{title, items:[{tid,name,bfl,kinds,subs:[{sid,name,bfl,kinds}]}]}]
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

    def _sub(s):
        return {"sid": str(s.id), "name": s.name, "bfl": s.is_bfl,
                "kinds": list(s.applicable_kinds or [])}

    def _node(t):
        subs = [_sub(s) for s in t.subtypes.all() if s.is_active]
        return {"tid": str(t.id), "name": t.name, "bfl": t.is_bfl,
                "kinds": list(t.applicable_kinds or []), "subs": subs}

    return [{"title": g["title"], "items": [_node(t) for t in g["items"]]} for g in groups]


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
    # Отдаём ВСЕ активные типы + подтипы; фильтр (БФЛ / вид процедуры) — на клиенте,
    # чтобы галочка «Показать все» его обходила.
    types = list(
        EfrsbMessageType.objects.filter(is_active=True)
        .prefetch_related("subtypes")
        .order_by("order", "name")
    )
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
        "proc_kind": (proc.kind if proc is not None else ""),
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


def _pick_type(request, *, required=True):
    """(mt, st, error_toast). Подтип обязателен, если у типа есть активные подтипы."""
    from .models import EfrsbMessageSubtype
    mt = EfrsbMessageType.objects.filter(pk=(request.POST.get("message_type") or None)).first()
    if mt is None:
        return None, None, _toast("Выберите тип сообщения.", "warning")
    st = EfrsbMessageSubtype.objects.filter(
        pk=(request.POST.get("subtype") or None), message_type=mt).first()
    if required and st is None and mt.subtypes.filter(is_active=True).exists():
        return None, None, _toast("Выберите подтип (тип судебного акта).", "warning")
    return mt, st, None


def _msg_modal(request, service, case, mt, st, *, pub=None, text="", error=""):
    """Модалка сообщения: только проблемные данные + генерация + текст + Сохранить."""
    from .generator import check_problems
    problems = check_problems(case, mt, st, procedure=case.current_procedure)
    return render(request, "efrsb/_publication_form_modal.html", {
        "service": service, "case": case, "mt": mt, "st": st, "pub": pub,
        "problems": problems, "text_value": text, "error": error,
        "type_label": (f"{mt.name} — {st.name}" if st else mt.name),
    })


@login_required
@require_procedures
@require_POST
def publication_add(request, service_id):
    """«+ Добавить сообщение» → модалка. Публикация создаётся только по «Сохранить»."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    mt, st, err = _pick_type(request)
    if err is not None:
        return err
    return _msg_modal(request, service, case, mt, st)


@never_cache
@login_required
@require_procedures
def publication_edit(request, service_id, pub_id):
    """Правка текста уже созданного сообщения (та же модалка)."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(EfrsbPublication, pk=pub_id, case=case)
    return _msg_modal(request, service, case, pub.message_type, pub.subtype,
                      pub=pub, text=pub.generated_text or "")


@login_required
@require_procedures
@require_POST
def publication_gen_text(request, service_id):
    """Кнопка «Сгенерировать текст» — подставляет CRM-данные в шаблон типа/подтипа."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    mt, st, err = _pick_type(request, required=False)
    if err is not None:
        return err
    from .generator import EfrsbGenError, render_message_text
    try:
        text = render_message_text(case, mt, st, procedure=case.current_procedure)
    except EfrsbGenError as exc:
        # Шаблона нет — оставляем текущий текст и подсказываем тостом.
        resp = render(request, "efrsb/_publication_text_field.html",
                      {"text_value": (request.POST.get("text") or "")})
        import json
        resp["HX-Trigger"] = json.dumps({"efrsbToast": {"msg": str(exc), "kind": "warning"}})
        return resp
    return render(request, "efrsb/_publication_text_field.html", {"text_value": text})


@login_required
@require_procedures
@require_POST
def publication_save(request, service_id):
    """«Сохранить»: создать/обновить публикацию с итоговым текстом + .docx/.pdf."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    mt, st, err = _pick_type(request)
    if err is not None:
        return err
    text = (request.POST.get("text") or "").strip()
    pub = None
    pub_id = request.POST.get("pub_id") or None
    if pub_id:
        pub = EfrsbPublication.objects.filter(pk=pub_id, case=case).first()

    from .generator import EfrsbGenError, save_publication
    if not text:
        return _msg_modal(request, service, case, mt, st, pub=pub, text=text,
                          error="Текст сообщения пуст — сгенерируйте его или введите вручную.")
    if pub is None:
        pub = EfrsbPublication.objects.create(
            case=case,
            procedure=case.current_procedure,
            message_type=mt,
            subtype=st,
            kind=(EfrsbPublication.KIND_REPORT
                  if mt.api_kind == EfrsbMessageType.API_KIND_REPORT
                  else EfrsbPublication.KIND_MESSAGE),
            origin=EfrsbPublication.ORIGIN_INTERNAL,
            status=EfrsbPublication.STATUS_DRAFT,
            title=(f"{mt.name} — {st.name}" if st else mt.name),
        )
    try:
        save_publication(pub, text, employee=_actor(request))
    except EfrsbGenError as exc:
        return _msg_modal(request, service, case, mt, st, pub=pub, text=text, error=str(exc))
    except Exception:
        log.exception("publication_save failed")
        return _msg_modal(request, service, case, mt, st, pub=pub, text=text,
                          error="Не удалось сохранить (ошибка конвертации в PDF).")
    # Пустой ответ + outerHTML-swap модалки = модалка закрывается.
    import json
    resp = HttpResponse("")
    resp["HX-Trigger"] = json.dumps({
        "reloadEfrsb": True,
        "efrsbToast": {"msg": "Сообщение сохранено. Текст можно скопировать из реестра.",
                       "kind": "success"},
    })
    return resp


@login_required
@require_procedures
@require_POST
def publication_check(request, service_id, pub_id):
    """Проверить в ЕФРСБ, опубликовано ли это сообщение (по должнику + типу/подтипу)."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(EfrsbPublication, pk=pub_id, case=case)
    from . import config, services
    from .client import EfrsbError
    if not config.is_configured():
        return _toast("ЕФРСБ не настроен (нет доступа к read-API).", "error")
    try:
        res = services.check_publication(pub)
    except EfrsbError as exc:
        return _toast(f"Ошибка ЕФРСБ: {exc}", "error")

    reason = res.get("reason")
    if res.get("found"):
        pub.refresh_from_db()
        msg = (f"Опубликовано в ЕФРСБ: № {pub.fedresurs_number or '—'}"
               f"{' от ' + pub.date_publish.strftime('%d.%m.%Y') if pub.date_publish else ''}.")
        if pub.has_violation:
            msg += " ⚠ С нарушением срока."
        return _toast(msg, "warning" if pub.has_violation else "success")
    if reason == "no_bankrupt_guid":
        return _toast("Должник не привязан к ЕФРСБ — сначала найдите его.", "warning")
    if reason == "no_type":
        return _toast("У публикации не задан тип сообщения.", "warning")
    return _toast("В ЕФРСБ пока не найдено — публикация не обнаружена.", "info")


@never_cache
@login_required
@require_procedures
def publication_text(request, service_id, pub_id):
    """Модалка с готовым текстом публикации (копировать → вставить в ЛК ЕФРСБ)."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(EfrsbPublication, pk=pub_id, case=case)
    return render(request, "efrsb/_publication_text_modal.html",
                  {"service": service, "pub": pub})


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
