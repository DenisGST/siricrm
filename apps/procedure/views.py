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
from apps.crm.models import Address, Client, ClientPhone, Service
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
    BankruptcyCase,
    MilestoneTemplate,
    Procedure,
    ProcedureMilestone,
    ProcedureStage,
    outcomes_for_kind,
)
from .permissions import require_procedures
from apps.core.permissions import is_references_access

PLACEHOLDER_TABS = {
    "correspondence": "Корреспонденция",
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

    # Финуправляющие — сотрудники с ролью «Арбитражный управляющий».
    managers = [
        {"id": e.id, "label": _mgr_label(e)}
        for e in Employee.objects.filter(role="arbitration", is_active=True).select_related("user")
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
    if fm_id and Employee.objects.filter(id=fm_id, role="arbitration").exists():
        p.financial_manager_id = fm_id
    else:
        p.financial_manager = None
    oc = request.POST.get("outcome", "")
    if oc not in {c for c, _ in outcomes_for_kind(p.kind)}:
        oc = ""
    p.outcome = oc
    p.save(update_fields=[
        "intro_date", "publication_efrsb_date", "publication_kommersant_date",
        "next_hearing_date", "end_date", "term_months", "financial_manager",
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
