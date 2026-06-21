"""Вьюхи раздела «Процедуры банкротства» — карточка дела по услуге БФЛ.

Карточка — полноэкранный экран, свопится в #content-area (как чат). Вкладки
грузятся HTMX-партиалами в #procedure-tab-body. Дело несёт общие стадии,
процедуры (реструктуризация/реализация) — дочерние, с собственными стадиями.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.core.models import Employee
from apps.crm.models import Address, Client, ClientPhone, Correspondence, LegalEntity, Service
from apps.crm.phone_utils import (
    add_client_phone,
    find_client_by_phone,
    normalize_phone,
    sync_client_phone_cache,
)

from . import services
from .models import (
    BASE_DATE_KEY_CHOICES,
    CLOSING_OUTCOMES,
    FIRST_HEARING_OUTCOMES,
    KIND_REALIZATION,
    KIND_RESTRUCTURING,
    PROCEDURE_KIND_CHOICES,
    SCOPE_COMMON,
    ArbitrationManager,
    BankruptcyCase,
    MilestoneTemplate,
    Procedure,
    ProcedureMilestone,
    ProcedureStage,
    Request,
    RequestPackage,
    RequestType,
    outcomes_for_kind,
)
from .permissions import require_procedures
from apps.core.permissions import is_references_access

PLACEHOLDER_TABS = {
    "documents": "Документы",
    "creditors": "Кредиторы / РТК",
    "publications": "Публикации",
}


class _NotBFL(Exception):
    """Внутренний сигнал → 403 (услуга не БФЛ)."""


def _actor(request):
    return getattr(request.user, "employee", None)


def _person_view(client):
    """Данные должника/супруги для отображения — из карточки Клиента."""
    if client is None:
        return None
    return {
        "id": client.id,
        "full_name": " ".join(filter(None, [
            client.last_name, client.first_name, client.patronymic])) or "—",
        "last_name": client.last_name or "",
        "first_name": client.first_name or "",
        "patronymic": client.patronymic or "",
        "birth_date": client.birth_date,
        "birth_place": client.birth_place or "",
        "passport_series": client.passport_series or "",
        "passport_number": client.passport_number or "",
        "passport_issued_by": client.passport_issued_by or "",
        "passport_issued_date": client.passport_issued_date,
        "passport_division_code": client.passport_division_code or "",
        "inn": client.inn or "",
        "snils": client.snils or "",
        "phones": client.phone or "",
    }


def _bfl_service(request, service_id) -> Service:
    service = get_object_or_404(
        Service.objects.visible_to(request.user).select_related("client", "name"),
        pk=service_id,
    )
    if service.name.short_name != "БФЛ":
        raise _NotBFL("Карточка процедуры доступна только для услуг БФЛ")
    return service


def _date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _fmt(d):
    return d.strftime("%Y-%m-%d") if d else ""


def build_timeline_phases(case: BankruptcyCase) -> list:
    """Таймлайны стадий по фазам (до введения / процедура(ы) / окончание).
    Рендерится в шапке карточки — блок `stages_bar`."""
    from apps.questionnaire.models import QuestionnaireResponse
    client = case.service.client
    qr = (QuestionnaireResponse.objects.filter(service=case.service)
          .order_by("created_at").first())
    date_application = (qr.created_at.date() if qr
                        else (client.created_at.date() if client.created_at else None))

    common_stages = list(ProcedureStage.objects.filter(
        kind_scope=SCOPE_COMMON, is_active=True, is_terminal=False).order_by("order"))
    terminal_stage = ProcedureStage.objects.filter(
        is_terminal=True, is_active=True).order_by("order").first()
    procedures = list(case.procedures.order_by("order"))
    cur_stage = case.current_stage_id
    cur_proc = case.current_procedure_id

    def _stage_date(code, proc):
        if code == "prep":
            return date_application
        if code == "filing":
            return case.filing_date
        if code == "accept":
            return case.first_hearing_date
        if proc is not None:
            if code in ("restr_start", "real_start"):
                return proc.intro_date
            if code in ("restr_run", "real_auction"):
                return proc.publication_efrsb_date
            if code in ("restr_done", "real_done"):
                return proc.end_date
        if code == "closed":
            last = case.procedures.exclude(end_date=None).order_by("-order").first()
            return last.end_date if last else None
        return None

    def _items(stages, proc):
        pid = str(proc.id) if proc else ""
        return [{
            "obj": st, "name": st.name, "procedure_id": pid,
            "is_current": st.id == cur_stage and (proc is None or proc.id == cur_proc),
            "date": _stage_date(st.code, proc),
        } for st in stages]

    phases = [{"label": "До введения процедуры", "items": _items(common_stages, None)}]
    for proc in procedures:
        pst = list(ProcedureStage.objects.filter(
            kind_scope=proc.kind, is_active=True, is_terminal=False).order_by("order"))
        phases.append({"label": proc.get_kind_display(), "items": _items(pst, proc)})
    if terminal_stage:
        phases.append({"label": "Окончание", "items": [{
            "obj": terminal_stage, "name": terminal_stage.name, "procedure_id": "",
            "is_current": terminal_stage.id == cur_stage,
            "date": _stage_date(terminal_stage.code, None),
        }]})
    flat = [it for ph in phases for it in ph["items"]]
    ci = next((i for i, it in enumerate(flat) if it["is_current"]), None)
    for i, it in enumerate(flat):
        it["state"] = ("done" if ci is not None and i < ci
                       else "current" if i == ci else "upcoming")
    return phases


def _overview_context(case: BankruptcyCase, expand_proc_id=None,
                      active_person_tab="debtor") -> dict:
    today = timezone.localdate()
    procedures = list(case.procedures.order_by("order"))

    # Многострочный таймлайн: строка общих стадий + строка на каждую процедуру.
    common_stages = list(
        ProcedureStage.objects.filter(
            kind_scope=SCOPE_COMMON, is_active=True, is_terminal=False
        ).order_by("order")
    )
    terminal_stage = ProcedureStage.objects.filter(
        is_terminal=True, is_active=True
    ).order_by("order").first()
    cur_stage = case.current_stage_id
    cur_proc = case.current_procedure_id

    def _mark(stages, proc_id):
        out = []
        for st in stages:
            is_current = st.id == cur_stage and (proc_id is None or proc_id == cur_proc)
            out.append({"obj": st, "is_current": is_current})
        return out

    rows = [{"label": "Общие стадии", "procedure": None,
             "stages": _mark(common_stages, None)}]
    for proc in procedures:
        stages = list(
            ProcedureStage.objects.filter(
                kind_scope=proc.kind, is_active=True, is_terminal=False
            ).order_by("order")
        )
        rows.append({"label": proc.get_kind_display(), "procedure": proc,
                     "stages": _mark(stages, proc.id)})

    terminal_is_current = bool(terminal_stage and terminal_stage.id == cur_stage)

    # Мероприятия с пометкой просрочки и группой.
    milestones = list(case.milestones.select_related("stage", "procedure", "template").all())
    overdue_count = 0
    for ms in milestones:
        ms.is_late = (
            ms.status == ProcedureMilestone.STATUS_OVERDUE
            or (ms.status == ProcedureMilestone.STATUS_PENDING
                and ms.due_date is not None and ms.due_date < today)
        )
        ms.group_label = ms.procedure.get_kind_display() if ms.procedure_id else "Общие"
        if ms.is_late:
            overdue_count += 1

    # Процедуры с форматированными датами и вариантами исхода.
    proc_cards = [{
        "obj": p,
        "intro": _fmt(p.intro_date),
        "pub_efrsb": _fmt(p.publication_efrsb_date),
        "pub_kommersant": _fmt(p.publication_kommersant_date),
        "next_hearing": _fmt(p.next_hearing_date),
        "end": _fmt(p.end_date),
        "outcome_choices": outcomes_for_kind(p.kind),
    } for p in procedures]

    def _mgr_label(e):
        name = " ".join(filter(None, [e.user.last_name, e.user.first_name, e.patronymic]))
        return name.strip() or e.user.get_full_name() or e.user.username

    # Финуправляющие — из справочника «Арбитражные управляющие».
    managers = [
        {"id": str(m.id), "label": m.full_fio}
        for m in ArbitrationManager.objects.filter(is_active=True)
    ]
    client = case.service.client
    spouse_client = client.spouse

    # Вычисляемые даты услуги (read-only):
    # 1 — обращение/анкета: дата анкеты услуги, иначе дата внесения клиента в базу.
    from apps.questionnaire.models import QuestionnaireResponse
    qr = (QuestionnaireResponse.objects.filter(service=case.service)
          .order_by("created_at").first())
    date_application = (qr.created_at.date() if qr
                        else (client.created_at.date() if client.created_at else None))
    # 4 — передача на подготовку иска: дата события «claim_prep_assigned».
    from apps.crm.models import ClientLogEntry
    e4 = (ClientLogEntry.objects.filter(client=client, event_type__code="claim_prep_assigned")
          .order_by("created_at").first())
    date_claim_prep = e4.created_at.date() if e4 else None

    # «+ Процедура»: для ПЕРВОЙ процедуры вид определяется итогом 1-го заседания;
    # для последующих — свободный выбор.
    _FIRST_KIND = {
        "fh_intro_restructuring": KIND_RESTRUCTURING,
        "fh_intro_realization": KIND_REALIZATION,
    }
    add_kind_locked = ""
    add_disabled = False
    add_disabled_reason = ""
    if not procedures:  # добавляем первую процедуру
        locked = _FIRST_KIND.get(case.first_hearing_outcome)
        if locked:
            add_kind_locked = locked
        else:
            add_disabled = True
            add_disabled_reason = (
                "Сначала укажите «Итог первого заседания»."
                if not case.first_hearing_outcome
                else "По итогу первого заседания процедура не вводится."
            )
    add_kind_locked_label = dict(PROCEDURE_KIND_CHOICES).get(add_kind_locked, "")

    return {
        "case": case,
        "service": case.service,
        "client": case.service.client,
        "rows": rows,
        "terminal_stage": terminal_stage,
        "terminal_is_current": terminal_is_current,
        "milestones": milestones,
        "overdue_count": overdue_count,
        "today": today,
        "first_hearing_outcomes": FIRST_HEARING_OUTCOMES,
        "kind_choices": PROCEDURE_KIND_CHOICES,
        "proc_cards": proc_cards,
        "case_filing_date": _fmt(case.filing_date),
        "case_claim_accept_date": _fmt(case.claim_accept_date),
        "case_first_hearing_date": _fmt(case.first_hearing_date),
        "date_dogovor": _fmt(case.service.date_dogovor),
        "case_docs_dept_date": _fmt(case.service.docs_dept_date),
        "date_application": date_application,
        "date_claim_prep": date_claim_prep,
        "add_kind_locked": add_kind_locked,
        "add_kind_locked_label": add_kind_locked_label,
        "add_disabled": add_disabled,
        "add_disabled_reason": add_disabled_reason,
        "expand_proc_id": str(expand_proc_id) if expand_proc_id else "",
        "managers": managers,
        "debtor": _person_view(client),
        "spouse": _person_view(spouse_client),
        "spouse_client": spouse_client,
        "active_person_tab": active_person_tab,
        "dadata_api_key": settings.DADATA_API_KEY,
    }


# ── Лендинг «Юрист БФЛ» (пункт меню) ───────────────────────────────────────

@never_cache
@login_required
@require_procedures
def panel(request):
    """Рабочее место юриста БФЛ — пустая карточка. Данные клиента подгружаются
    выбором в главном поиске (кнопка «Дело БФЛ» в строке клиента)."""
    return render(request, "procedure/panel.html", {})


@never_cache
@login_required
@require_procedures
def open_client_case(request):
    """Открыть дело БФЛ клиента в рабочей области (из кнопки в поиске).
    0 услуг БФЛ → подсказка; 1 → сразу карточка; несколько → выбор."""
    client_id = request.GET.get("client_id")
    svcs = list(
        Service.objects.visible_to(request.user)
        .select_related("client", "name")
        .filter(client_id=client_id, name__short_name="БФЛ")
        .order_by("-date_dogovor", "-id")
    )
    if not svcs:
        return render(request, "procedure/panel.html", {"no_bfl": True})
    if len(svcs) == 1:
        return procedure_card(request, svcs[0].id)
    return render(request, "procedure/panel_pick.html",
                  {"client": svcs[0].client, "services": svcs})


# ── Карточка + вкладки ─────────────────────────────────────────────────────

@never_cache
@login_required
@require_procedures
def procedure_card(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    services.ensure_case(service)
    return render(request, "procedure/card.html", {
        "service": service, "client": service.client,
        "placeholder_tabs": PLACEHOLDER_TABS,
    })


@never_cache
@login_required
@require_procedures
def tab_overview(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    return render(request, "procedure/_tab_overview.html", _overview_context(case))


@never_cache
@login_required
@require_procedures
def stages_bar(request, service_id):
    """Таймлайны стадий для шапки карточки (грузится лениво, обновляется
    по событию procStagesChanged после действий, меняющих стадии/даты)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    return render(request, "procedure/_stages_bar.html", {
        "service": service, "timeline_phases": build_timeline_phases(case),
    })


@never_cache
@login_required
@require_procedures
def tab_court(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    return render(request, "procedure/_tab_court.html", {
        "service": service, "case": getattr(service, "arbitr_case", None),
    })


@never_cache
@login_required
@require_procedures
def tab_placeholder(request, service_id, tab):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    label = PLACEHOLDER_TABS.get(tab)
    if not label:
        return HttpResponseBadRequest("Неизвестная вкладка")
    return render(request, "procedure/_tab_placeholder.html", {"service": service, "label": label})


# ── Действия (POST) — возвращают перерисованную вкладку «Обзор» ─────────────

def _reload(request, case, expand_proc_id=None, person_tab="debtor"):
    resp = render(request, "procedure/_tab_overview.html",
                  _overview_context(case, expand_proc_id, active_person_tab=person_tab))
    # Обновить таймлайн в шапке (он вне #procedure-tab-body).
    resp["HX-Trigger"] = "procStagesChanged"
    return resp


@login_required
@require_procedures
@require_POST
def update_case_block(request, service_id):
    """Сохранение СВОДКИ по делу (даты дела/услуги + итог 1-го заседания).
    Процедуры сохраняются отдельно (update_procedure)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)

    case.filing_date = _date(request.POST.get("filing_date"))
    case.claim_accept_date = _date(request.POST.get("claim_accept_date"))
    case.first_hearing_date = _date(request.POST.get("first_hearing_date"))
    fh_outcome = request.POST.get("first_hearing_outcome", "")
    if fh_outcome not in {c for c, _ in FIRST_HEARING_OUTCOMES}:
        fh_outcome = ""
    case.first_hearing_outcome = fh_outcome
    case.save(update_fields=[
        "filing_date", "claim_accept_date",
        "first_hearing_date", "first_hearing_outcome", "updated_at",
    ])
    # Даты услуги: договор (п.2) + передача в отдел сбора документов (п.3).
    service.date_dogovor = _date(request.POST.get("date_dogovor"))
    service.docs_dept_date = _date(request.POST.get("docs_dept_date"))
    service.save(update_fields=["date_dogovor", "docs_dept_date"])

    services.recompute_case_closed(case)
    services.recompute_due_dates(case)
    return _reload(request, case)


@login_required
@require_procedures
@require_POST
def update_procedure(request, service_id, proc_id):
    """Сохранение полей ОДНОЙ процедуры (своя форма «Сохранить»)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    p = get_object_or_404(Procedure, pk=proc_id, case=case)
    p.intro_date = _date(request.POST.get("intro_date"))
    p.publication_efrsb_date = _date(request.POST.get("publication_efrsb_date"))
    p.publication_kommersant_date = _date(request.POST.get("publication_kommersant_date"))
    p.next_hearing_date = _date(request.POST.get("next_hearing_date"))
    p.end_date = _date(request.POST.get("end_date"))
    term = (request.POST.get("term_months") or "").strip()
    p.term_months = int(term) if term.isdigit() else None
    fm_id = request.POST.get("financial_manager") or ""
    p.arbitr_manager = ArbitrationManager.objects.filter(id=fm_id).first() if fm_id else None
    oc = request.POST.get("outcome", "")
    if oc not in {c for c, _ in outcomes_for_kind(p.kind)}:
        oc = ""
    p.outcome = oc
    p.save(update_fields=[
        "intro_date", "publication_efrsb_date", "publication_kommersant_date",
        "next_hearing_date", "end_date", "term_months", "arbitr_manager",
        "outcome", "updated_at",
    ])
    services.recompute_case_closed(case)
    services.recompute_due_dates(case)
    return _reload(request, case)


@login_required
@require_procedures
@require_POST
def add_procedure(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    kind = request.POST.get("kind")
    # Первая процедура: вид жёстко определяется итогом первого заседания.
    if not case.procedures.exists():
        forced = {
            "fh_intro_restructuring": KIND_RESTRUCTURING,
            "fh_intro_realization": KIND_REALIZATION,
        }.get(case.first_hearing_outcome)
        if not forced:
            return HttpResponseBadRequest(
                "Вид первой процедуры определяется итогом первого заседания")
        kind = forced
    if kind not in {c for c, _ in PROCEDURE_KIND_CHOICES}:
        return HttpResponseBadRequest("Неизвестный вид процедуры")
    proc = services.add_procedure(
        case, kind, intro_date=_date(request.POST.get("intro_date")), employee=_actor(request),
    )
    # Новая процедура открыта сразу для заполнения.
    return _reload(request, case, expand_proc_id=proc.id)


@login_required
@require_procedures
@require_POST
def delete_procedure(request, service_id, proc_id):
    """Удалить процедуру (с её мероприятиями — каскадом)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    p = get_object_or_404(Procedure, pk=proc_id, case=case)
    p.delete()  # current_procedure обнулится (SET_NULL), мероприятия — каскадом
    services.recompute_case_closed(case)
    return _reload(request, case)


@login_required
@require_procedures
@require_POST
def set_stage(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    stage = get_object_or_404(ProcedureStage, pk=request.POST.get("stage_id"))
    proc = None
    proc_id = request.POST.get("procedure_id")
    if proc_id:
        proc = get_object_or_404(Procedure, pk=proc_id, case=case)
    services.enter_stage(case, stage, procedure=proc, employee=_actor(request))
    return _reload(request, case)


@login_required
@require_procedures
@require_POST
def milestone_set_status(request, pk):
    ms = get_object_or_404(
        ProcedureMilestone.objects.select_related("case__service"), pk=pk,
    )
    if not Service.objects.visible_to(request.user).filter(pk=ms.case.service_id).exists():
        return HttpResponseForbidden("Нет доступа")
    status = request.POST.get("status", ProcedureMilestone.STATUS_DONE)
    if status not in {s for s, _ in ProcedureMilestone.STATUS_CHOICES}:
        return HttpResponseBadRequest("Неизвестный статус")
    services.set_milestone_status(ms, status, employee=_actor(request))
    return _reload(request, ms.case)


@login_required
@require_procedures
@require_POST
def milestone_add(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    title = request.POST.get("title", "").strip()
    if not title:
        return HttpResponseBadRequest("Пустое название мероприятия")
    proc = None
    proc_id = request.POST.get("procedure_id")
    if proc_id:
        proc = get_object_or_404(Procedure, pk=proc_id, case=case)
    services.add_manual_milestone(
        case, title=title, procedure=proc, due_date=_date(request.POST.get("due_date")),
    )
    return _reload(request, case)


# ── Данные должника/супруги (правка карточки Client) ────────────────────────

@login_required
@require_procedures
@require_POST
def update_person(request, service_id, who):
    """Сохранить данные должника (who=debtor) или супруги (who=spouse) в карточку Client."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    if who == "debtor":
        client = service.client
    elif who == "spouse":
        client = service.client.spouse
    else:
        return HttpResponseBadRequest("Неизвестно чьи данные")
    if client is None:
        return HttpResponseBadRequest("Нет записи")

    p = request.POST
    client.last_name = p.get("last_name", "").strip()
    client.first_name = p.get("first_name", "").strip()
    client.patronymic = p.get("patronymic", "").strip()
    client.birth_date = _date(p.get("birth_date"))
    client.birth_place = p.get("birth_place", "").strip()
    client.passport_series = p.get("passport_series", "").strip()
    client.passport_number = p.get("passport_number", "").strip()
    client.passport_issued_by = p.get("passport_issued_by", "").strip()
    client.passport_issued_date = _date(p.get("passport_issued_date"))
    client.passport_division_code = p.get("passport_division_code", "").strip()
    client.inn = p.get("inn", "").strip()
    client.snils = p.get("snils", "").strip()
    client.save(update_fields=[
        "last_name", "first_name", "patronymic", "birth_date", "birth_place",
        "passport_series", "passport_number", "passport_issued_by",
        "passport_issued_date", "passport_division_code", "inn", "snils",
    ])
    # «Сумма всех долгов» — поле услуги (показывается в табе «Должник»).
    if who == "debtor":
        raw = (p.get("total_debt") or "").replace(" ", "").replace(",", ".")
        try:
            service.total_debt = Decimal(raw) if raw else None
        except (InvalidOperation, ValueError):
            service.total_debt = None
        service.save(update_fields=["total_debt"])
    return _reload(request, case, person_tab=who if who == "spouse" else "debtor")


# ── Супруга (Client.spouse) ─────────────────────────────────────────────────

@login_required
@require_procedures
def spouse_search(request, service_id):
    """Поиск существующих клиентов для выбора супруги (typeahead)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    q = (request.GET.get("q") or "").strip()
    clients = Client.objects.none()
    if len(q) >= 2:
        clients = (
            Client.objects.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q)
                | Q(patronymic__icontains=q) | Q(phone__icontains=q)
            ).exclude(pk=service.client_id).distinct()
            .order_by("last_name", "first_name")[:15]
        )
    return render(request, "procedure/_spouse_search_results.html", {
        "service": service, "clients": clients, "query": q,
    })


@login_required
@require_procedures
def spouse_pick(request, service_id):
    """Превью выбранного клиента-супруги перед сохранением (нужно подтвердить кнопкой)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    c = get_object_or_404(Client, pk=request.GET.get("client_id"))
    return render(request, "procedure/_spouse_pick.html", {"service": service, "c": c})


@login_required
@require_procedures
@require_POST
def spouse_link(request, service_id):
    """Привязать существующего клиента как супругу (Client.spouse, взаимно)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    spouse = get_object_or_404(Client, pk=request.POST.get("client_id"))
    client = service.client
    if spouse.pk != client.pk:
        client.spouse = spouse
        client.is_married = True
        client.save(update_fields=["spouse", "is_married"])
        spouse.spouse = client
        spouse.is_married = True
        spouse.save(update_fields=["spouse", "is_married"])
    return _reload(request, case, person_tab="spouse")


@login_required
@require_procedures
@require_POST
def spouse_create(request, service_id):
    """Создать новую запись клиента-супруги и привязать (Client.spouse)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    client = service.client
    # Пустая запись — поля заполняются в полной форме супруга после создания.
    spouse = Client.objects.create(first_name="", is_married=True, spouse=client)
    client.spouse = spouse
    client.is_married = True
    client.save(update_fields=["spouse", "is_married"])
    return _reload(request, case, person_tab="spouse")


# ── Телефоны должника/супруги (CRUD как в карточке клиента) ──────────────────

def _person_client(service, who):
    if who == "debtor":
        return service.client
    if who == "spouse":
        return service.client.spouse
    return None


def _render_phones(request, service, who, error=""):
    client = _person_client(service, who)
    return render(request, "procedure/_phones_block.html", {
        "service": service,
        "who": who,
        "client": client,
        "phones": client.phones.order_by("purpose", "phone") if client else [],
        "purpose_choices": ClientPhone.PURPOSE_CHOICES,
        "error": error,
    })


@login_required
@require_procedures
def phones_block(request, service_id, who):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    return _render_phones(request, service, who)


@login_required
@require_procedures
@require_POST
def phones_add(request, service_id, who):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    client = _person_client(service, who)
    if client is None:
        return HttpResponseBadRequest("Нет записи")
    raw = (request.POST.get("phone") or "").strip()
    purpose = (request.POST.get("purpose") or "additional").strip()
    if purpose not in dict(ClientPhone.PURPOSE_CHOICES):
        return HttpResponseBadRequest("bad purpose")
    phone = normalize_phone(raw)
    if not phone:
        return _render_phones(request, service, who, error=f"Неверный номер: {raw}")
    other = find_client_by_phone(phone)
    if other is not None and other.pk != client.pk:
        fio = f"{other.last_name} {other.first_name}".strip() or "без ФИО"
        return _render_phones(request, service, who,
                              error=f"+{phone} уже у клиента «{fio}» — дубликат запрещён.")
    obj = add_client_phone(client, phone, purpose)
    if obj is None:
        return _render_phones(request, service, who,
                              error=f"+{phone} уже занят в этом назначении.")
    sync_client_phone_cache(client)
    return _render_phones(request, service, who)


@login_required
@require_procedures
@require_POST
def phones_delete(request, service_id, who, phone_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    client = _person_client(service, who)
    cp = get_object_or_404(ClientPhone, pk=phone_id, client=client)
    cp.delete()
    sync_client_phone_cache(client)
    return _render_phones(request, service, who)


@login_required
@require_procedures
@require_POST
def phones_set_purpose(request, service_id, who, phone_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    client = _person_client(service, who)
    cp = get_object_or_404(ClientPhone, pk=phone_id, client=client)
    purpose = (request.POST.get("purpose") or "").strip()
    if purpose not in dict(ClientPhone.PURPOSE_CHOICES):
        return HttpResponseBadRequest("bad purpose")
    conflict = ClientPhone.objects.filter(
        phone=cp.phone, purpose=purpose,
    ).exclude(pk=cp.pk).first()
    if conflict:
        return _render_phones(request, service, who,
                              error=f"+{cp.phone} в этом назначении уже занят.")
    cp.purpose = purpose
    cp.save(update_fields=["purpose", "updated_at"])
    sync_client_phone_cache(client)
    return _render_phones(request, service, who)


# ── Адреса должника/супруги (полный CRUD как в карточке клиента) ─────────────

def _render_addresses(request, service, who):
    client = _person_client(service, who)
    return render(request, "procedure/_address_block.html", {
        "service": service, "who": who, "client": client,
        "addresses": client.addresses.order_by("address_type") if client else [],
    })


@login_required
@require_procedures
def addresses_block(request, service_id, who):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    return _render_addresses(request, service, who)


@login_required
@require_procedures
def address_form(request, service_id, who, address_id=None):
    """GET — DaData-форма адреса; POST — сохранение → список (как у клиента)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    client = _person_client(service, who)
    if client is None:
        return HttpResponseBadRequest("Нет записи")
    address = get_object_or_404(Address, pk=address_id, client=client) if address_id else None

    from apps.crm.views import DADATA_ADDRESS_FIELDS
    if request.method == "POST":
        addr = address or Address(client=client)
        addr.address_type = request.POST.get("address_type", "default")
        addr.comment = request.POST.get("comment", "")
        addr.source = request.POST.get("source", "")
        addr.result = request.POST.get("result", "") or addr.source
        for field in DADATA_ADDRESS_FIELDS:
            setattr(addr, field, request.POST.get(field, ""))
        addr.save()
        return _render_addresses(request, service, who)

    obj = address or Address()
    return render(request, "procedure/_address_form.html", {
        "service": service, "who": who, "client": client,
        "address": obj,
        "addr_fields": [(f, getattr(obj, f, "")) for f in DADATA_ADDRESS_FIELDS],
        "address_types": Address.ADDRESS_TYPES,
        "dadata_api_key": settings.DADATA_API_KEY,
        "is_new": address is None,
    })


@login_required
@require_procedures
@require_POST
def address_delete(request, service_id, who, address_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    client = _person_client(service, who)
    addr = get_object_or_404(Address, pk=address_id, client=client)
    addr.delete()
    return _render_addresses(request, service, who)


# ── Справочник «Шаблоны мероприятий» (редактирование вне админки) ────────────
# Гейт — `is_references_access` (как у остальных справочников: superuser/admin/
# head_dep), раздел открывается из «Справочников». Каталог стадий редактируется
# отдельно (пока в админке) — здесь только мероприятия.

@user_passes_test(is_references_access)
def references_milestones(request):
    items = (
        MilestoneTemplate.objects.select_related("stage")
        .order_by("stage__order", "order", "title")
    )
    return render(request, "procedure/partials/references_milestones.html", {"items": items})


@user_passes_test(is_references_access)
def reference_milestone_edit(request, pk=None):
    from .forms import MilestoneTemplateForm
    obj = get_object_or_404(MilestoneTemplate, pk=pk) if pk else None
    if request.method == "POST":
        form = MilestoneTemplateForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadMilestones"})
    else:
        form = MilestoneTemplateForm(instance=obj)
    return render(request, "procedure/partials/milestone_form_modal.html", {
        "form": form, "obj": obj,
        "stages": ProcedureStage.objects.filter(is_active=True).order_by("order"),
        "base_date_choices": BASE_DATE_KEY_CHOICES,
    })


@user_passes_test(is_references_access)
@require_POST
def reference_milestone_delete(request, pk):
    # template→ProcedureMilestone.on_delete=SET_NULL → у живых процедур
    # мероприятие остаётся в истории (FK обнуляется).
    get_object_or_404(MilestoneTemplate, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadMilestones"})


# ── Вкладка «Корреспонденция» → Запросы ─────────────────────────────────────

def _req_trigger():
    """Пустой ответ + сигнал перезагрузить список запросов."""
    return HttpResponse(headers={"HX-Trigger": "reloadRequests"})


def _correspondence_context(case) -> dict:
    today = timezone.localdate()
    requests = list(case.requests.select_related("recipient", "request_type").all())
    for r in requests:
        r.is_late = bool(r.status == Request.STATUS_SENT and r.due_date and r.due_date < today)
    corr = (Correspondence.objects.filter(service=case.service)
            .select_related("counterparty", "stored_file", "request")
            .order_by("-sent_at", "-created_at"))
    # Судебные акты — все вложения из арбитражного дела (мониторинг kad).
    from apps.arbitr.models import ArbitrAttachment
    arb = getattr(case.service, "arbitr_case", None)
    court_acts = []
    if arb is not None:
        court_acts = list(
            ArbitrAttachment.objects.filter(event__case=arb)
            .select_related("event").order_by("-event__event_date", "-created_at")
        )
    return {
        "service": case.service,
        "case": case,
        "today": today,
        "requests": requests,
        "incoming": [c for c in corr if c.direction == "incoming"],
        "outgoing": [c for c in corr if c.direction == "outgoing"],
        "court_acts": court_acts,
        "has_arbitr_case": arb is not None,
        "request_types": RequestType.objects.filter(is_active=True).order_by("order", "name"),
        "request_packages": RequestPackage.objects.filter(is_active=True).order_by("order", "name"),
        "method_choices": Request.METHOD_CHOICES,
    }


@never_cache
@login_required
@require_procedures
def tab_correspondence(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    return render(request, "procedure/_tab_correspondence.html", _correspondence_context(case))


@login_required
@require_procedures
def recipient_search(request, service_id):
    """Typeahead госоргана — поиск по реестру LegalEntity (имя/ИНН).
    Доп. фильтр по типу (kind) и приоритет региона дела (region=1)."""
    q = (request.GET.get("q") or "").strip()
    items = []
    if len(q) >= 2:
        qs = LegalEntity.objects.filter(
            Q(name__icontains=q) | Q(short_name__icontains=q) | Q(inn__icontains=q)
        )
        kind = (request.GET.get("kind") or "").strip()
        if kind:
            qs = qs.filter(kind_id=kind)
        if request.GET.get("region") == "1":
            region_id = (Service.objects.filter(pk=service_id)
                         .values_list("region_id", flat=True).first())
            if region_id:
                from django.db.models import Case, IntegerField, When
                qs = qs.annotate(_rm=Case(
                    When(region_id=region_id, then=0), default=1,
                    output_field=IntegerField())).order_by("_rm", "name")
            else:
                qs = qs.order_by("name")
        else:
            qs = qs.order_by("name")
        items = list(qs.select_related("region")[:10])
    return render(request, "procedure/_recipient_results.html", {"items": items, "q": q})


@login_required
@require_procedures
@require_POST
def request_add(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    rt = get_object_or_404(RequestType, pk=request.POST.get("request_type"))
    recipient = None
    rid = (request.POST.get("recipient_id") or "").strip()
    if rid:
        recipient = LegalEntity.objects.filter(pk=rid).first()
    services.create_request(case, rt, recipient=recipient, employee=_actor(request))
    return _req_trigger()


@login_required
@require_procedures
@require_POST
def request_package_add(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    pkg = get_object_or_404(RequestPackage, pk=request.POST.get("package"))
    services.create_request_package(case, pkg, employee=_actor(request))
    return _req_trigger()


@login_required
@require_procedures
@require_POST
def request_delete(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    get_object_or_404(Request, pk=req_id, case=case).delete()
    return _req_trigger()


@never_cache
@login_required
@require_procedures
def request_sent_form(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    return render(request, "procedure/_request_sent_modal.html", {
        "service": service, "req": req, "method_choices": Request.METHOD_CHOICES,
    })


@login_required
@require_procedures
@require_POST
def request_sent(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    services.mark_request_sent(
        req, method=request.POST.get("sent_method", ""),
        sent_date=_date(request.POST.get("sent_date")), employee=_actor(request),
    )
    return _req_trigger()


@never_cache
@login_required
@require_procedures
def request_response_form(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    return render(request, "procedure/_request_response_modal.html", {"service": service, "req": req})


@login_required
@require_procedures
@require_POST
def request_response(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    services.set_request_response(
        req,
        response_date=_date(request.POST.get("response_date")),
        number=request.POST.get("response_number", ""),
        text=request.POST.get("response_text", ""),
        no_answer=bool(request.POST.get("no_answer")),
        employee=_actor(request),
    )
    f = request.FILES.get("response_scan")
    if f:
        req.response_scan = _scan_to_storedfile(f)
        req.save(update_fields=["response_scan", "updated_at"])
    return _req_trigger()


# ── Справочники «Типы запросов» / «Пакеты запросов» ─────────────────────────

@user_passes_test(is_references_access)
def reference_recipient_search(request):
    """Typeahead госоргана для справочника типов (без привязки к услуге)."""
    q = (request.GET.get("q") or "").strip()
    items = []
    if len(q) >= 2:
        items = list(
            LegalEntity.objects.filter(
                Q(name__icontains=q) | Q(short_name__icontains=q) | Q(inn__icontains=q)
            ).order_by("name")[:10]
        )
    return render(request, "procedure/_recipient_results.html", {"items": items, "q": q})


@user_passes_test(is_references_access)
def references_request_types(request):
    items = RequestType.objects.select_related("default_recipient").order_by("order", "name")
    return render(request, "procedure/partials/references_request_types.html", {"items": items})


@user_passes_test(is_references_access)
def reference_request_type_edit(request, pk=None):
    from .forms import RequestTypeForm
    obj = get_object_or_404(RequestType, pk=pk) if pk else None
    if request.method == "POST":
        form = RequestTypeForm(request.POST, instance=obj)
        if form.is_valid():
            o = form.save(commit=False)
            rid = (request.POST.get("recipient_id") or "").strip()
            o.default_recipient = LegalEntity.objects.filter(pk=rid).first() if rid else None
            o.save()
            return HttpResponse(headers={"HX-Trigger": "reloadRequestTypes"})
    else:
        form = RequestTypeForm(instance=obj)
    from apps.afd.models import DocumentTemplate
    return render(request, "procedure/partials/request_type_form_modal.html", {
        "form": form, "obj": obj,
        "doc_templates": DocumentTemplate.objects.filter(
            kind=DocumentTemplate.KIND_REQUEST, is_active=True).order_by("name"),
    })


@user_passes_test(is_references_access)
@require_POST
def reference_request_type_delete(request, pk):
    get_object_or_404(RequestType, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadRequestTypes"})


@user_passes_test(is_references_access)
def references_request_packages(request):
    items = RequestPackage.objects.prefetch_related("types").order_by("order", "name")
    return render(request, "procedure/partials/references_request_packages.html", {"items": items})


@user_passes_test(is_references_access)
def reference_request_package_edit(request, pk=None):
    from .forms import RequestPackageForm
    obj = get_object_or_404(RequestPackage, pk=pk) if pk else None
    if request.method == "POST":
        form = RequestPackageForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadRequestPackages"})
    else:
        form = RequestPackageForm(instance=obj)
    if request.method == "POST":
        selected = request.POST.getlist("types")
    else:
        selected = [str(t.pk) for t in obj.types.all()] if obj else []
    return render(request, "procedure/partials/request_package_form_modal.html", {
        "form": form, "obj": obj, "selected_type_ids": selected,
        "all_types": RequestType.objects.order_by("order", "name"),
    })


@user_passes_test(is_references_access)
@require_POST
def reference_request_package_delete(request, pk):
    get_object_or_404(RequestPackage, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadRequestPackages"})


# ── Справочник «Арбитражные управляющие» ────────────────────────────────────

@user_passes_test(is_references_access)
def references_managers(request):
    items = ArbitrationManager.objects.select_related("sro", "employee__user").order_by("last_name", "first_name")
    return render(request, "procedure/partials/references_managers.html", {"items": items})


@user_passes_test(is_references_access)
def reference_manager_edit(request, pk=None):
    from .forms import ArbitrationManagerForm
    obj = get_object_or_404(ArbitrationManager, pk=pk) if pk else None
    if request.method == "POST":
        form = ArbitrationManagerForm(request.POST, instance=obj)
        if form.is_valid():
            o = form.save(commit=False)
            rid = (request.POST.get("recipient_id") or "").strip()
            o.sro = LegalEntity.objects.filter(pk=rid).first() if rid else None
            sig = request.FILES.get("signature")
            if sig:
                o.signature_file = _scan_to_storedfile(sig)
            o.save()
            return HttpResponse(headers={"HX-Trigger": "reloadManagers"})
    else:
        form = ArbitrationManagerForm(instance=obj)
    from apps.crm.models import LegalEntityKind
    sro_kind = (LegalEntityKind.objects.filter(short_name__iexact="СРО").first()
                or LegalEntityKind.objects.filter(name__icontains="аморегулир").first())
    sro_options = (LegalEntity.objects.filter(kind=sro_kind).order_by("name")
                   if sro_kind else LegalEntity.objects.none())
    return render(request, "procedure/partials/manager_form_modal.html", {
        "form": form, "obj": obj, "sro_options": sro_options,
        "employees": Employee.objects.filter(is_active=True)
        .select_related("user").order_by("user__last_name", "user__first_name"),
    })


@user_passes_test(is_references_access)
@require_POST
def reference_manager_delete(request, pk):
    get_object_or_404(ArbitrationManager, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadManagers"})


# ── Запросы: формирование документа ─────────────────────────────────────────

@never_cache
@login_required
@require_procedures
def request_generate_form(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    from .request_documents import check_request_data
    all_ok, check_groups = check_request_data(req)
    return render(request, "procedure/_request_generate_modal.html", {
        "service": service, "req": req,
        "check_all_ok": all_ok, "check_groups": check_groups,
    })


@login_required
@require_procedures
@require_POST
def request_generate(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    from .request_documents import RequestDocError, generate_request_document
    try:
        generate_request_document(
            req,
            with_signature=bool(request.POST.get("with_signature")),
            marriage_cert=(request.POST.get("marriage_cert") or "").strip(),
            employee=_actor(request),
        )
    except RequestDocError as exc:
        return render(request, "procedure/_request_generate_modal.html",
                      {"service": service, "req": req, "error": str(exc)})
    except Exception:
        import logging
        logging.getLogger(__name__).exception("request_generate failed")
        return render(request, "procedure/_request_generate_modal.html",
                      {"service": service, "req": req,
                       "error": "Не удалось сформировать документ (ошибка конвертации). Попробуйте ещё раз."})
    return _req_trigger()


# ── Корреспонденция: загрузка сканов (Входящие/Исходящие) ───────────────────

def _scan_to_storedfile(f):
    """Загрузить файл-скан в S3 → StoredFile (+ ссылка для предпросмотра)."""
    from apps.files.models import StoredFile
    from apps.files.s3_utils import upload_file_to_s3
    data = f.read()
    bucket, key = upload_file_to_s3(
        data, prefix="procedure/correspondence", filename=f.name,
        content_type=(f.content_type or "application/octet-stream"),
    )
    return StoredFile.objects.create(
        bucket=bucket, key=key, filename=f.name,
        content_type=(f.content_type or ""), size=len(data),
    )


@never_cache
@login_required
@require_procedures
def correspondence_upload_form(request, service_id, direction):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    if direction not in ("incoming", "outgoing"):
        return HttpResponseBadRequest("Неизвестное направление")
    case = services.ensure_case(service)
    from apps.crm.models import LegalEntityKind
    # Для входящих — список запросов дела (привязать ответ к запросу).
    case_requests = (case.requests.select_related("recipient").order_by("outgoing_number")
                     if direction == "incoming" else [])
    return render(request, "procedure/_correspondence_upload_modal.html",
                  {"service": service, "direction": direction,
                   "kinds": LegalEntityKind.objects.order_by("name"),
                   "case_requests": case_requests})


@login_required
@require_procedures
@require_POST
def correspondence_upload(request, service_id, direction):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    if direction not in ("incoming", "outgoing"):
        return HttpResponseBadRequest("Неизвестное направление")
    services.ensure_case(service)
    from django.urls import reverse
    co = Correspondence(
        service=service, direction=direction,
        subject_type=(request.POST.get("subject_type") or "").strip(),
        outgoing_number=(request.POST.get("number") or "").strip(),
        sent_at=_date(request.POST.get("date")),
        comments=(request.POST.get("comments") or "").strip(),
    )
    rid = (request.POST.get("recipient_id") or "").strip()
    if rid:
        co.counterparty = LegalEntity.objects.filter(pk=rid).first()
    f = request.FILES.get("scan")
    sf = None
    if f:
        sf = _scan_to_storedfile(f)
        co.file_link = reverse("files:stored_download", args=[sf.id]) + "?inline=1"
    co.save()
    # Привязка входящего к запросу как ответ на него.
    if direction == "incoming" and request.POST.get("as_response"):
        rq = Request.objects.filter(
            pk=(request.POST.get("request_id") or ""), case__service=service).first()
        if rq is not None:
            if sf is not None:
                rq.response_scan = sf
            rq.response_date = co.sent_at or rq.response_date
            num = (request.POST.get("number") or "").strip()
            if num:
                rq.response_number = num
            rq.status = Request.STATUS_ANSWERED
            rq.save(update_fields=[
                "response_scan", "response_date", "response_number", "status", "updated_at",
            ])
    return _req_trigger()


# ── Запросы: онлайн-редактирование документа (текст по абзацам) ──────────────

@never_cache
@login_required
@require_procedures
def request_edit_form(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    paras = []
    if req.document_docx_id:
        from apps.files.s3_utils import download_file_from_s3
        from .request_documents import extract_editable_paragraphs
        data = download_file_from_s3(req.document_docx.bucket, req.document_docx.key)
        paras = extract_editable_paragraphs(data)
    return render(request, "procedure/_request_edit_modal.html",
                  {"service": service, "req": req, "paras": paras})


@login_required
@require_procedures
@require_POST
def request_edit_save(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    if not req.document_docx_id:
        return HttpResponseBadRequest("Нет документа для редактирования")
    from apps.files.s3_utils import download_file_from_s3
    from .request_documents import apply_paragraph_edits, save_edited_document
    edits = {}
    for k, v in request.POST.items():
        if k.startswith("p_"):
            try:
                edits[int(k[2:])] = v
            except ValueError:
                pass
    data = download_file_from_s3(req.document_docx.bucket, req.document_docx.key)
    try:
        new_docx = apply_paragraph_edits(data, edits)
        save_edited_document(req, new_docx, employee=_actor(request))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("request_edit_save failed")
        return render(request, "procedure/_request_edit_modal.html", {
            "service": service, "req": req,
            "paras": [{"index": int(k[2:]), "text": v} for k, v in request.POST.items() if k.startswith("p_")],
            "error": "Не удалось сохранить (ошибка конвертации). Попробуйте ещё раз.",
        })
    return _req_trigger()


# ── Превью офисных файлов (doc/docx/xls…) через Microsoft Office Online Viewer ─

@login_required
@require_procedures
def doc_presigned_url(request, service_id, sf_id):
    """Pre-signed URL файла в S3 — сырой публичный (Office Online Viewer его сам
    скачивает с серверов Microsoft, поэтому наш auth-gated stored_download не годится)."""
    from django.http import JsonResponse
    from apps.files.models import StoredFile
    from apps.files.s3_utils import get_presigned_url
    sf = get_object_or_404(StoredFile, pk=sf_id)
    return JsonResponse({"url": get_presigned_url(sf.bucket, sf.key, expiration=1800)})


# ── Запросы: подгрузка готового документа (pdf/docx) ────────────────────────

@never_cache
@login_required
@require_procedures
def request_upload_form(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    return render(request, "procedure/_request_upload_doc_modal.html", {"service": service, "req": req})


@login_required
@require_procedures
@require_POST
def request_upload(request, service_id, req_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    f = request.FILES.get("file")
    name = (getattr(f, "name", "") or "").lower()
    if not f or not (name.endswith(".pdf") or name.endswith(".docx")):
        return render(request, "procedure/_request_upload_doc_modal.html",
                      {"service": service, "req": req, "error": "Загрузите файл .pdf или .docx"})
    from apps.files.models import StoredFile
    from apps.files.s3_utils import upload_file_to_s3
    from .request_documents import DOCX_CT, _attach, _store
    is_docx = name.endswith(".docx")
    data = f.read()
    ct = f.content_type or (DOCX_CT if is_docx else "application/pdf")
    bucket, key = upload_file_to_s3(data, prefix="procedure/requests", filename=f.name, content_type=ct)
    sf = StoredFile.objects.create(bucket=bucket, key=key, filename=f.name, content_type=ct, size=len(data))
    client = req.case.service.client
    emp = _actor(request)
    _attach(client, sf, emp)
    if is_docx:
        req.document_docx = sf
        try:
            from apps.afd.pdf_utils import docx_to_pdf
            pdf_sf = _store(docx_to_pdf(data), filename=f"{f.name[:-5]}.pdf", content_type="application/pdf")
            _attach(client, pdf_sf, emp)
            req.document_pdf = pdf_sf
        except Exception:
            import logging
            logging.getLogger(__name__).exception("request_upload: docx→pdf failed")
    else:
        req.document_pdf = sf
    req.generated_at = timezone.now()
    req.save(update_fields=["document_docx", "document_pdf", "generated_at", "updated_at"])
    return _req_trigger()
