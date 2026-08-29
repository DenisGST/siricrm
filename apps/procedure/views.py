"""Вьюхи раздела «Процедуры банкротства» — карточка дела по услуге БФЛ.

Карточка — полноэкранный экран, свопится в #content-area (как чат). Вкладки
грузятся HTMX-партиалами в #procedure-tab-body. Дело несёт общие стадии,
процедуры (реструктуризация/реализация) — дочерние, с собственными стадиями.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import F, Q, Value
from django.db.models.functions import NullIf
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_sameorigin
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
    ALL_OUTCOMES,
    BASE_DATE_KEY_CHOICES,
    CLOSING_OUTCOMES,
    FIRST_HEARING_OUTCOMES,
    KIND_REALIZATION,
    KIND_RESTRUCTURING,
    PROCEDURE_KIND_CHOICES,
    PROCEDURE_OUTCOME_CHOICES,
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
    "creditors": "Кредиторы / РТК",
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


# ── Лендинг «Юрист БФЛ»: сводная таблица процедур ──────────────────────────
#
# Строка таблицы = процедура (реструктуризация/реализация). У дела процедур
# может быть несколько — тогда и строк несколько. Дело, где процедура ещё не
# введена (подготовка/подача/первое заседание), тоже даёт строку — с
# псевдо-видом KIND_NONE и прочерками вместо дат.
#
# Универсум строк — услуги БФЛ, видимые пользователю (`Service.visible_to`),
# а НЕ `BankruptcyCase`: запись дела создаётся лениво (`services.ensure_case`)
# при первом открытии карточки, и по делам заводится не сразу.
#
# 🛑 Фильтрация, сортировка и пагинация — ЦЕЛИКОМ на стороне БД (LIMIT/OFFSET).
# Строка выборки = строка LEFT JOIN к процедурам, поэтому постраничная выборка
# честная: из БД приезжает ровно страница. Раньше все ~6000 строк тянулись в
# питон на каждый запрос.

KIND_NONE = "none"                      # псевдо-вид: процедура не введена
PROC_PATH = "bankruptcy_case__procedures"

CASES_PER_PAGE_CHOICES = (25, 50, 100)
CASES_DEFAULT_PER_PAGE = 25
CASES_DEFAULT_SORT = "intro"
CASES_DEFAULT_DIR = "desc"

# Состояние процедуры — главный фильтр. По умолчанию показываем только те,
# что ещё идут: процедура введена (строка есть) и исход не проставлен.
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_NO_PROC = "no_proc"
STATE_ALL = "all"
CASES_STATE_CHOICES = [
    (STATE_RUNNING, "Незавершённые процедуры"),
    (STATE_DONE, "Завершённые процедуры"),
    (STATE_NO_PROC, "Процедура не введена"),
    (STATE_ALL, "Все строки"),
]
CASES_DEFAULT_STATE = STATE_RUNNING
OUTCOME_CODES = [code for code, _ in PROCEDURE_OUTCOME_CHOICES]

# Колонки плоской выборки — порядок ВАЖЕН, распаковывается кортежем ниже.
CASES_VALUES = (
    "id",
    "client__last_name", "client__first_name", "client__patronymic",
    "arbitr_case__case_number", "arbitr_case__kad_url",
    "bankruptcy_case__status",
    f"{PROC_PATH}__kind",
    f"{PROC_PATH}__intro_date",
    f"{PROC_PATH}__next_hearing_date",
    f"{PROC_PATH}__publication_kommersant_date",
    f"{PROC_PATH}__outcome",
)
CASES_KIND_LABELS = dict(PROCEDURE_KIND_CHOICES)
CASES_STATUS_LABELS = dict(BankruptcyCase.STATUS_CHOICES)

# Столбец → выражения ORDER BY. Пустые значения всегда внизу (nulls_last),
# иначе сортировка по редкой колонке выдаёт страницу сплошных прочерков.
# Пустая строка — не NULL, поэтому № дела прогоняем через NULLIF.
CASES_SORT_EXPRESSIONS = {
    "fio": lambda: [F("client__last_name"), F("client__first_name"),
                    F("client__patronymic")],
    "case_number": lambda: [NullIf(F("arbitr_case__case_number"), Value(""))],
    "intro": lambda: [F(f"{PROC_PATH}__intro_date")],
    "hearing": lambda: [F(f"{PROC_PATH}__next_hearing_date")],
    "kommersant": lambda: [F(f"{PROC_PATH}__publication_kommersant_date")],
    "kind": lambda: [F(f"{PROC_PATH}__kind")],
}


def _cases_order_by(sort: str, direction: str) -> list:
    """ORDER BY для сводной таблицы + стабильный тай-брейк.

    🛑 Тай-брейк обязателен: без него Postgres волен отдавать строки с равным
    ключом (а таких много — половина колонок пустая) в разном порядке на
    разных OFFSET, и при листании страницы дублировались бы/терялись.
    """
    desc = direction == "desc"
    order = [
        expr.desc(nulls_last=True) if desc else expr.asc(nulls_last=True)
        for expr in CASES_SORT_EXPRESSIONS[sort]()
    ]
    order += [F("id").asc(), F(f"{PROC_PATH}__order").asc(nulls_last=True)]
    return order


def _cases_queryset(request, *, q="", state=CASES_DEFAULT_STATE, kind="",
                    status="", mine=False, sort=CASES_DEFAULT_SORT,
                    direction=CASES_DEFAULT_DIR):
    """Queryset строк сводной таблицы (кортежи `CASES_VALUES`).

    🛑 Все условия по пути `bankruptcy_case__procedures` собираются в ОДИН
    `filter()`: на многозначной связи каждый отдельный `filter()` создаёт новый
    JOIN, и «незавершённая» + «реализация» матчились бы на РАЗНЫХ процедурах
    одного дела. Один Q → один join, тот же, из которого читает `values_list`.
    """
    # Видимость — подзапросом по pk, а не join'ом: `visible_to` для рядового
    # сотрудника джойнит M2M и размножает строки, а нам размножение нужно
    # только по процедурам (иначе пришлось бы городить distinct поверх
    # ORDER BY-выражений).
    visible = (
        Service.objects.visible_to(request.user)
        .filter(name__short_name="БФЛ")
        .values("pk")
    )
    qs = Service.objects.filter(pk__in=visible)

    emp = _actor(request)
    if mine and emp is not None:
        qs = qs.filter(pk__in=Service.objects.filter(employees=emp).values("pk"))

    for word in q.split():
        qs = qs.filter(
            Q(client__last_name__icontains=word)
            | Q(client__first_name__icontains=word)
            | Q(client__patronymic__icontains=word)
            | Q(arbitr_case__case_number__icontains=word)
        )

    if status == BankruptcyCase.STATUS_ACTIVE:
        # Услуга без записи дела — тоже «в работе»: `ensure_case` ленивый.
        qs = qs.filter(Q(bankruptcy_case__status=status)
                       | Q(bankruptcy_case__isnull=True))
    elif status:
        qs = qs.filter(bankruptcy_case__status=status)

    proc_q = Q()
    if state == STATE_RUNNING:
        proc_q &= Q(**{f"{PROC_PATH}__isnull": False, f"{PROC_PATH}__outcome": ""})
    elif state == STATE_DONE:
        proc_q &= Q(**{f"{PROC_PATH}__outcome__in": OUTCOME_CODES})
    elif state == STATE_NO_PROC:
        proc_q &= Q(**{f"{PROC_PATH}__isnull": True})
    if kind:
        proc_q &= Q(**{f"{PROC_PATH}__kind": kind})
    if proc_q:
        qs = qs.filter(proc_q)

    return qs.values_list(*CASES_VALUES).order_by(*_cases_order_by(sort, direction))


def _cases_rows(tuples, offset: int) -> list:
    """Кортежи выборки → строки шаблона (нумерация сквозная по страницам)."""
    rows = []
    for i, (service_id, last_name, first_name, patronymic, case_number, kad_url,
            case_status, proc_kind, intro_date, hearing_date, kommersant_date,
            outcome) in enumerate(tuples, start=offset + 1):
        fio = " ".join(filter(None, [last_name, first_name, patronymic])).strip()
        rows.append({
            "n": i,
            "service_id": service_id,
            "client_fio": fio or "—",
            "case_number": case_number or "",
            "kad_url": kad_url or "",
            "case_status": case_status or BankruptcyCase.STATUS_ACTIVE,
            "case_status_label": CASES_STATUS_LABELS.get(
                case_status or BankruptcyCase.STATUS_ACTIVE, ""),
            "kind": proc_kind or KIND_NONE,
            "kind_label": CASES_KIND_LABELS.get(proc_kind, "Процедура не введена"),
            "intro_date": intro_date,
            "hearing_date": hearing_date,
            "kommersant_date": kommersant_date,
            "outcome_label": ALL_OUTCOMES.get(outcome or "", ""),
        })
    return rows


def _cases_context(request) -> dict:
    """Контекст сводной таблицы: фильтры + поиск + сортировка + страница."""
    q = (request.GET.get("q") or "").strip()
    state = (request.GET.get("state") or "").strip()
    if state not in dict(CASES_STATE_CHOICES):
        state = CASES_DEFAULT_STATE
    kind = (request.GET.get("kind") or "").strip()
    if kind not in CASES_KIND_LABELS:
        kind = ""
    status = (request.GET.get("status") or "").strip()
    if status not in CASES_STATUS_LABELS:
        status = ""
    mine = (request.GET.get("mine") or "") in ("1", "on", "true")
    sort = request.GET.get("sort") or CASES_DEFAULT_SORT
    if sort not in CASES_SORT_EXPRESSIONS:
        sort = CASES_DEFAULT_SORT
    direction = request.GET.get("dir")
    if direction not in ("asc", "desc"):
        direction = CASES_DEFAULT_DIR

    try:
        per_page = int(request.GET.get("per_page") or CASES_DEFAULT_PER_PAGE)
    except (TypeError, ValueError):
        per_page = CASES_DEFAULT_PER_PAGE
    if per_page not in CASES_PER_PAGE_CHOICES:
        per_page = CASES_DEFAULT_PER_PAGE

    qs = _cases_queryset(request, q=q, state=state, kind=kind, status=status,
                         mine=mine, sort=sort, direction=direction)
    paginator = Paginator(qs, per_page)          # COUNT + LIMIT/OFFSET
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return {
        "rows": _cases_rows(page_obj.object_list, (page_obj.number - 1) * per_page),
        "page_obj": page_obj,
        "paginator": paginator,
        "page_range": list(paginator.get_elided_page_range(
            page_obj.number, on_each_side=1, on_ends=1)),
        "ellipsis": Paginator.ELLIPSIS,
        "total": paginator.count,
        "q": q, "state": state, "kind": kind, "status": status, "mine": mine,
        "has_employee": _actor(request) is not None,
        "sort": sort, "direction": direction,
        "per_page": per_page,
        "per_page_choices": CASES_PER_PAGE_CHOICES,
        "state_choices": CASES_STATE_CHOICES,
        "default_state": CASES_DEFAULT_STATE,
        "kind_choices": PROCEDURE_KIND_CHOICES,
        "status_choices": BankruptcyCase.STATUS_CHOICES,
        "filters_active": bool(q or kind or status or mine
                               or state != CASES_DEFAULT_STATE),
    }


@never_cache
@login_required
@require_procedures
def panel(request):
    """Рабочее место юриста БФЛ — сводная таблица процедур банкротства.
    Карточка конкретного дела открывается кликом по строке."""
    return render(request, "procedure/panel.html", _cases_context(request))


@never_cache
@login_required
@require_procedures
def cases_table(request):
    """HTMX-партиал сводной таблицы (фильтры/поиск/сортировка/страница).

    Свопится в #proc-cases-table — панель фильтров не перерисовывается, поэтому
    фокус в поле поиска не теряется. Текущие sort/dir возвращаются в скрытые
    поля формы OOB-свопом (`oob`), чтобы смена фильтра их не сбрасывала.
    """
    ctx = _cases_context(request)
    ctx["oob"] = True
    return render(request, "procedure/_cases_table.html", ctx)


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
        ctx = _cases_context(request)
        ctx["no_bfl"] = True
        return render(request, "procedure/panel.html", ctx)
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


# Разрешённые поля сортировки таблицы запросов → ключ сортировки.
# None-значения всегда в хвосте (независимо от направления).
_REQ_STATUS_ORDER = {s[0]: i for i, s in enumerate(Request.STATUS_CHOICES)}


def _req_sort_key(field):
    """Возвращает функцию-ключ (значение, is_none) для сортировки списка запросов."""
    def by(val_getter):
        def key(r):
            v = val_getter(r)
            return (v is None or v == "", v)
        return key
    getters = {
        "number": lambda r: r.outgoing_number,
        "created": lambda r: r.created_at,
        "type": lambda r: (r.title or "").lower(),
        "recipient": lambda r: (r.recipient_display or "").lower(),
        "sent": lambda r: r.sent_date,
        "due": lambda r: r.due_date,
        "status": lambda r: _REQ_STATUS_ORDER.get(r.status, 99),
    }
    return by(getters.get(field, getters["created"]))


def _attach_counts(model, objects) -> dict:
    """{pk: число вложений} — одним запросом на таблицу (иначе N+1 на строку)."""
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Count

    from .models import DocumentAttachment
    if not objects:
        return {}
    ct = ContentType.objects.get_for_model(model)
    rows = (DocumentAttachment.objects
            .filter(content_type=ct, object_id__in=[o.pk for o in objects])
            .values("object_id").annotate(n=Count("id")))
    return {r["object_id"]: r["n"] for r in rows}


def _requests_context(case, req_q="", req_sort="created", req_dir="desc") -> dict:
    """Отфильтрованный + отсортированный список запросов дела для таблицы.

    Общая логика для полной вкладки и отдельной перезагрузки таблицы
    (поиск/сортировка). Фильтрация/сортировка — в Python (запросов по делу
    немного), т.к. recipient_display — property, а не поле БД.
    """
    today = timezone.localdate()
    if req_sort not in {"number", "created", "type", "recipient", "sent", "due", "status"}:
        req_sort = "created"
    if req_dir not in ("asc", "desc"):
        req_dir = "desc"
    requests = list(case.requests.select_related("recipient", "request_type").all())
    for r in requests:
        r.is_late = bool(r.status == Request.STATUS_SENT and r.due_date and r.due_date < today)
    q = (req_q or "").strip().lower()
    if q:
        def _match(r):
            hay = " ".join(str(x) for x in (
                r.outgoing_number or "", r.title or "", r.recipient_display or "",
                r.get_status_display(), r.response_number or "",
            )).lower()
            return q in hay
        requests = [r for r in requests if _match(r)]
    requests.sort(key=_req_sort_key(req_sort), reverse=(req_dir == "desc"))
    counts = _attach_counts(Request, requests)
    for r in requests:
        r.attach_count = counts.get(r.pk, 0)
    return {
        "today": today,
        "requests": requests,
        "req_q": req_q,
        "req_sort": req_sort,
        "req_dir": req_dir,
    }


def _correspondence_context(case, req_q="", req_sort="created", req_dir="desc") -> dict:
    today = timezone.localdate()
    req_ctx = _requests_context(case, req_q, req_sort, req_dir)
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
    corr = list(corr)
    corr_counts = _attach_counts(Correspondence, corr)
    for c in corr:
        c.attach_count = corr_counts.get(c.pk, 0)
    return {
        "service": case.service,
        "case": case,
        "incoming": [c for c in corr if c.direction == "incoming"],
        "outgoing": [c for c in corr if c.direction == "outgoing"],
        "court_acts": court_acts,
        "has_arbitr_case": arb is not None,
        "request_types": RequestType.objects.filter(is_active=True).order_by("order", "name"),
        "method_choices": Request.METHOD_CHOICES,
        **req_ctx,
    }


def _req_params(request):
    """Читает параметры поиска/сортировки таблицы запросов из GET."""
    return (
        (request.GET.get("req_q") or "").strip(),
        (request.GET.get("req_sort") or "created").strip(),
        (request.GET.get("req_dir") or "desc").strip(),
    )


@never_cache
@login_required
@require_procedures
def tab_correspondence(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    ctx = _correspondence_context(case, *_req_params(request))
    return render(request, "procedure/_tab_correspondence.html", ctx)


@never_cache
@login_required
@require_procedures
def requests_table(request, service_id):
    """Только таблица запросов (для поиска/сортировки без перезагрузки всей вкладки)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    ctx = _requests_context(case, *_req_params(request))
    ctx["service"] = service
    return render(request, "procedure/_requests_table.html", ctx)


def _resolve_hint(reason: str, n: int) -> str:
    """Человекочитаемая подсказка по итогу автоподбора адресата."""
    return {
        "none": "Адресат не требуется (Росреестр — через СМЭВ).",
        "fns_code": "Инспекция подобрана по коду ИФНС из адреса клиента.",
        "remembered": "Адресат из запомненного правила для региона/района.",
        "region_unique": "Единственный орган вида в регионе клиента.",
        "region_office": "Подобрано областное управление (адресат уровня региона).",
        "district_unique": "Подобран по району/городу клиента.",
        "district_many": f"В районе клиента подходит {n} — уточните выбор из списка.",
        "region_many": f"В регионе {n} органов — выберите из списка (фильтр по типу уже задан).",
        "no_region": "У клиента не определён регион — выберите адресата вручную.",
        "manual": "Выберите адресата вручную из реестра.",
    }.get(reason, "")


@login_required
@require_procedures
def recipient_search(request, service_id):
    """Typeahead госоргана по реестру LegalEntity — с фильтром по типу запроса.

    `type` (RequestType) → вид ЮЛ (kind) фильтром + регион клиента приоритетом.
    Пустой `q` при заданном типе → короткий список органов вида в регионе клиента
    (тот самый удобный отфильтрованный выбор). Явный `kind` тоже поддерживается.
    """
    from django.db.models import Case, IntegerField, When
    from .recipient_resolver import client_region

    q = (request.GET.get("q") or "").strip()
    kind_id = (request.GET.get("kind") or "").strip()

    type_id = (request.GET.get("type") or "").strip()
    if type_id:
        rt = RequestType.objects.filter(pk=type_id).select_related("recipient_kind").first()
        if rt and rt.recipient_kind_id:
            kind_id = str(rt.recipient_kind_id)

    service = (Service.objects.filter(pk=service_id)
               .select_related("client").first())
    region = client_region(service.client, service) if service else None

    qs = LegalEntity.objects.filter(is_active=True)
    if kind_id:
        qs = qs.filter(kind_id=kind_id)

    items = []
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(short_name__icontains=q) | Q(inn__icontains=q)
        )
        if region:
            qs = qs.annotate(_rm=Case(
                When(region_id=region.id, then=0), default=1,
                output_field=IntegerField())).order_by("_rm", "name")
        else:
            qs = qs.order_by("name")
        items = list(qs.select_related("region")[:12])
    elif kind_id and region:
        # Без текста, но задан тип+регион → показать органы вида в регионе.
        items = list(qs.filter(region=region)
                     .select_related("region").order_by("name")[:40])
    return render(request, "procedure/_recipient_results.html", {"items": items, "q": q})


@login_required
@require_procedures
def recipient_picker(request, service_id):
    """Большая модалка выбора госоргана из справочника (фильтр по виду+региону, поиск)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    from .recipient_resolver import client_region
    rt = (RequestType.objects.filter(pk=request.GET.get("type"))
          .select_related("recipient_kind").first())
    region = client_region(service.client, service)
    return render(request, "procedure/partials/recipient_picker_modal.html", {
        "service": service, "rt": rt,
        "kind": rt.recipient_kind if (rt and rt.recipient_kind_id) else None,
        "region": region,
    })


@login_required
@require_procedures
def recipient_picker_search(request, service_id):
    """Строки таблицы госорганов для модалки выбора (имя/регион/адрес/ИНН)."""
    from django.db.models import Case, IntegerField, When
    from .recipient_resolver import client_region
    q = (request.GET.get("q") or "").strip()
    kind_id = (request.GET.get("kind") or "").strip()
    only_region = request.GET.get("region") == "1"
    service = (Service.objects.filter(pk=service_id).select_related("client").first())
    region = client_region(service.client, service) if service else None

    qs = LegalEntity.objects.filter(is_active=True).select_related("region", "kind")
    if kind_id:
        qs = qs.filter(kind_id=kind_id)
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(short_name__icontains=q) | Q(inn__icontains=q))
    if only_region and region:
        qs = qs.filter(region=region).order_by("name")
    elif region:
        qs = qs.annotate(_rm=Case(
            When(region_id=region.id, then=0), default=1,
            output_field=IntegerField())).order_by("_rm", "name")
    else:
        qs = qs.order_by("name")
    items = list(qs[:80])
    return render(request, "procedure/partials/_recipient_picker_rows.html",
                  {"items": items, "service_id": service_id})


@login_required
@require_procedures
def request_resolve(request, service_id):
    """Автоподбор адресата для выбранного типа запроса (JSON для префилла в UI)."""
    from django.http import JsonResponse
    from .recipient_resolver import resolve_recipient
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    rt = RequestType.objects.filter(pk=request.GET.get("type")).select_related("recipient_kind").first()
    if not rt:
        return JsonResponse({"ok": False})
    res = resolve_recipient(rt, service.client, service)
    rec = res["recipient"]
    reason = res["reason"]
    n = len(res["candidates"])
    rec_addr = ""
    if rec:
        rec_addr = rec.legal_address or rec.actual_address or rec.postal_address or ""
    return JsonResponse({
        "ok": True,
        "recipient_id": str(rec.pk) if rec else "",
        "recipient_name": ((rec.short_name or rec.name) if rec else ""),
        "recipient_address": rec_addr,
        "hint": _resolve_hint(reason, n),
        "reason": reason,
        "count": n,
        # «Запомнить для региона/района» имеет смысл только для подбора по
        # региону (МРЭО/ЗАГС/суд/ДМИ/ГИМС…). Банк (manual) и ФНС (по адресу) —
        # не региональные, для них запоминание правила бессмысленно.
        "can_remember": (bool(rt.recipient_kind_id)
                         and rt.recipient_lookup == RequestType.LOOKUP_REGION),
    })


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
    # Тип «уведомление кредиторам» разворачивается в письмо на каждого кредитора.
    services.create_requests_for_type(
        case, rt, recipient=recipient, employee=_actor(request))
    # Запомнить ручной выбор адресата для (вид + регион/район) → переиспользуется.
    if recipient and (request.POST.get("remember") in ("1", "on", "true")):
        services.save_recipient_rule(
            rt, service.client, service, recipient, employee=_actor(request))
    return _req_trigger()


def _main_package():
    """Единый пакет запросов (выбора пакета в UI больше нет — он один)."""
    return (RequestPackage.objects.filter(is_active=True)
            .order_by("order", "name").first())


@login_required
@require_procedures
def request_package_modal(request, service_id):
    """Модалка пакета: предпроверка общих сведений дела + таблица всех позиций
    с чекбоксом (по умолчанию все отмечены), авто-подобранным адресатом (меняется
    через большую модалку выбора) и подсветкой готовности каждой позиции."""
    from .recipient_resolver import resolve_recipient
    from .request_documents import check_case_data, check_request_ready
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    pkg = _main_package()
    if pkg is None:
        return HttpResponseBadRequest(
            "Пакет запросов не настроен — заведите его в Справочниках.")

    # 1) Общие сведения (должник / ФУ / дело) — предупреждение в шапке модалки.
    case_ok, case_gaps = check_case_data(case)

    creditors_n = len(services.case_creditors(service))
    rows = []
    for rt in pkg.types.filter(is_active=True).order_by("order", "name"):
        lookup = rt.recipient_lookup
        rec, note, hint = None, "", ""
        if lookup == RequestType.LOOKUP_DEBTOR:
            # Адресат — сам должник: ФИО и адрес берутся из карточки клиента.
            note = services.debtor_display(service.client) or "у клиента не заполнено ФИО"
        elif lookup == RequestType.LOOKUP_CREDITORS:
            # Разворачивается в письмо на каждого кредитора из анкеты.
            note = (f"будет создано писем: {creditors_n} (по числу кредиторов)"
                    if creditors_n else "кредиторы в анкете не найдены — писем не будет")
        elif lookup != RequestType.LOOKUP_NONE:
            res = resolve_recipient(rt, service.client, service)
            rec = res["recipient"]
            hint = _resolve_hint(res["reason"], len(res["candidates"]))
        # 2) Готовность позиции: шаблон + адресат + все плейсхолдеры заполнены.
        ready, issues = check_request_ready(
            case, rt, rec, creditors_count=creditors_n)
        rows.append({
            "type": rt,
            "recipient": rec,
            "recipient_name": ((rec.short_name or rec.name) if rec else ""),
            "recipient_address": ((rec.legal_address or rec.actual_address
                                   or rec.postal_address or "") if rec else ""),
            "hint": hint,
            "lookup": lookup,
            "note": note,
            "ready": ready,
            "issues": issues,
            # Позиции, где адресата выбирает юрист (для перекраски чека на лету).
            "needs_recipient": lookup in (
                RequestType.LOOKUP_REGION, RequestType.LOOKUP_FNS,
                RequestType.LOOKUP_MANUAL),
            # Готовность без учёта адресата — чтобы после выбора в модалке
            # перекрасить чек в зелёный, не перезагружая всю таблицу.
            "data_ok": not [i for i in issues if not i.startswith("не выбран адресат")],
            "can_remember": (bool(rt.recipient_kind_id)
                             and rt.recipient_lookup == RequestType.LOOKUP_REGION),
        })
    # Подпись/печать ФУ есть → чекбокс «с подписью» включён по умолчанию.
    from .request_documents import _am_procedure
    proc = _am_procedure(case)
    am = proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None
    return render(request, "procedure/partials/request_package_modal.html", {
        "service": service, "package": pkg, "rows": rows,
        "case_ok": case_ok, "case_gaps": case_gaps,
        "ready_count": sum(1 for r in rows if r["ready"]),
        "has_signature": bool(am and am.signature_file_id),
    })


@login_required
@require_procedures
@require_POST
def request_package_add(request, service_id):
    """Создать запросы пакета и СРАЗУ сформировать документы (в Celery).

    Черновиков без документа не оставляем: на каждый созданный запрос ставится
    своя задача формирования (LibreOffice → PDF → файлы клиента). Возвращаем не
    «готово», а блок прогресса — он сам поллит статус, пока всё не сформируется.
    """
    from .tasks import JOB_TTL, generate_request_doc, job_meta_key
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    pkg = _main_package()
    if pkg is None:
        return HttpResponseBadRequest("Пакет запросов не настроен.")
    # Отмеченные позиции + адресаты, выбранные в модалке, + какие запомнить.
    type_ids, recipients, to_remember = set(), {}, []
    for rt in pkg.types.filter(is_active=True):
        if request.POST.get(f"include_{rt.pk}") not in ("1", "on", "true"):
            continue
        type_ids.add(str(rt.pk))
        rid = (request.POST.get(f"recipient_id_{rt.pk}") or "").strip()
        rec = LegalEntity.objects.filter(pk=rid).first() if rid else None
        recipients[str(rt.pk)] = rec
        if rec and (request.POST.get(f"remember_{rt.pk}") in ("1", "on", "true")):
            to_remember.append((rt, rec))

    employee = _actor(request)
    created = services.create_request_package(
        case, pkg, employee=employee, recipients=recipients, type_ids=type_ids)
    for rt, rec in to_remember:
        services.save_recipient_rule(rt, service.client, service, rec, employee=employee)

    if not created:
        return _req_trigger()

    with_signature = request.POST.get("with_signature") in ("1", "on", "true")
    job_id = uuid.uuid4().hex
    cache.set(job_meta_key(job_id), {
        "service_id": str(service.id),
        "items": [{"id": str(r.pk), "title": r.title,
                   "number": r.outgoing_number,
                   "recipient": r.recipient_display} for r in created],
    }, JOB_TTL)
    for r in created:
        generate_request_doc.delay(
            job_id, str(r.pk),
            employee_id=(str(employee.pk) if employee else None),
            with_signature=with_signature,
        )
    return render(request, "procedure/partials/_package_progress.html",
                  _progress_context(service, job_id))


def _progress_context(service, job_id) -> dict:
    from .tasks import job_status
    meta, items, counters = job_status(job_id)
    return {
        "service": service, "job": job_id,
        "items": items, "c": counters, "expired": meta is None,
    }


@never_cache
@login_required
@require_procedures
def package_progress(request, service_id):
    """Прогресс формирования документов пакета (HTMX-поллинг раз в 1.5с)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    ctx = _progress_context(service, (request.GET.get("job") or "").strip())
    resp = render(request, "procedure/partials/_package_progress.html", ctx)
    # Всё сформировано → обновить таблицу запросов под модалкой (документы, PDF).
    if not ctx["expired"] and not ctx["c"].get("running"):
        resp["HX-Trigger"] = "reloadRequests"
    return resp


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


@login_required
@require_procedures
@require_POST
def request_batch_delete(request, service_id):
    """Пакетное удаление запросов (чекбоксы в таблице)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    ids = request.POST.getlist("req_ids")
    if ids:
        Request.objects.filter(case=case, pk__in=ids).delete()
    return _req_trigger()


@xframe_options_sameorigin
@login_required
@require_procedures
def request_envelope(request, service_id, req_id):
    """Почтовый конверт (Почта РФ) для одного запроса — inline PDF + в файлы клиента.

    🛑 xframe_options_sameorigin — глобально X_FRAME_OPTIONS=DENY, а PDF отдаём
    прямо из Django (не редиректом на S3, как stored_download), и он открывается
    в iframe модалки procPreview с нашего же домена.
    """
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    from .request_envelopes import envelope_for_request
    size = (request.GET.get("size") or "C5").strip()
    pdf = envelope_for_request(req, size=size, employee=_actor(request))
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = 'inline; filename="envelope.pdf"'
    return resp


@xframe_options_sameorigin
@login_required
@require_procedures
def case_envelopes(request, service_id):
    """Конверты на все запросы дела (у кого есть адресат) — один многостраничный PDF.

    🛑 xframe_options_sameorigin — см. request_envelope (PDF в iframe procPreview).
    """
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    from .request_envelopes import envelopes_for_case
    size = (request.GET.get("size") or "C5").strip()
    pdf, made, skipped = envelopes_for_case(case, size=size, employee=_actor(request))
    if not pdf:
        return HttpResponse(
            "<div style='padding:24px;font-family:sans-serif'>Нет запросов с указанным "
            "адресатом — сначала подберите госорганы в запросах.</div>",
            content_type="text/html; charset=utf-8",
        )
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = 'inline; filename="envelopes.pdf"'
    return resp


# ── Выгрузка для Почты России (файл загрузки заказов в ЛК otpravka.pochta.ru) ──

def _pochta_selection(request, case):
    """Разбирает параметры выгрузки (общее для модалки предпросмотра и скачивания).

    Построчные правки приходят как `mass:<key>` (вес в граммах) и `notify:<key>`
    (галочка «с уведомлением») — ключ строки задаётся в pochta_export._item.
    """
    src = request.POST if request.method == "POST" else request.GET
    ids = src.getlist("req_ids")
    reqs = list(
        Request.objects.filter(case=case, pk__in=ids)
        .select_related("recipient", "request_type").order_by("outgoing_number")
    ) if ids else []
    mass = {}
    for name, value in src.items():
        if not name.startswith("mass:"):
            continue
        try:
            grams = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if grams > 0:
            mass[name[len("mass:"):]] = grams
    return {
        "ids": ids,
        "requests": reqs,
        "with_creditors": src.get("creditors") == "1",
        "with_debtor": src.get("debtor") == "1",
        "mass_overrides": mass,
        "notify_keys": src.getlist("notify_keys"),
    }


@never_cache
@login_required
@require_procedures
@require_POST
def requests_pochta_modal(request, service_id):
    """Предпросмотр выгрузки для Почты: что уйдёт в файл, что отсеяно и почему."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    sel = _pochta_selection(request, case)
    from . import pochta_export

    items = pochta_export.collect_items(
        case, requests=sel["requests"], with_creditors=sel["with_creditors"],
        with_debtor=sel["with_debtor"], mass_overrides=sel["mass_overrides"],
        notify_keys=sel["notify_keys"],
    )
    # split_ready проставляет `problems` прямо в строках — в модалке показываем
    # единым списком: вес и галочку уведомления юрист правит у любой строки,
    # в том числе чтобы снять перевес.
    ready, skipped = pochta_export.split_ready(items)
    return render(request, "procedure/_pochta_export_modal.html", {
        "service": service, "case": case, "sel": sel,
        "rows": items, "ready": ready, "skipped": skipped,
        "index_from": pochta_export.index_from_for_case(case),
    })


@never_cache
@login_required
@require_procedures
def requests_pochta_export(request, service_id):
    """Файл загрузки заказов для ЛК otpravka.pochta.ru (.xlsx по шаблону Почты)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    sel = _pochta_selection(request, case)
    from . import pochta_export

    xlsx, ready, skipped = pochta_export.export_case(
        case, requests=sel["requests"],
        with_creditors=sel["with_creditors"], with_debtor=sel["with_debtor"],
        mass_overrides=sel["mass_overrides"], notify_keys=sel["notify_keys"],
    )
    if not xlsx:
        return HttpResponse(
            "<div style='padding:24px;font-family:sans-serif'>Нечего выгружать: "
            "у выбранных адресатов не хватает реквизитов (наименование, адрес, индекс).</div>",
            content_type="text/html; charset=utf-8",
        )
    fio = (service.client.last_name or "клиент").strip()
    filename = f"Почта {fio} ({len(ready)} шт).xlsx"
    resp = HttpResponse(
        xlsx,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    # 🛑 Кириллица в имени файла — только через RFC 5987 (filename*), иначе
    # браузер получит мусор в заголовке.
    resp["Content-Disposition"] = (
        f"attachment; filename=\"pochta.xlsx\"; filename*=UTF-8''{quote(filename)}"
    )
    return resp


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
    from apps.crm.models import LegalEntityKind
    return render(request, "procedure/partials/request_type_form_modal.html", {
        "form": form, "obj": obj,
        "doc_templates": DocumentTemplate.objects.filter(
            kind=DocumentTemplate.KIND_REQUEST, is_active=True).order_by("name"),
        "entity_kinds": LegalEntityKind.objects.order_by("short_name"),
        "lookup_choices": RequestType.LOOKUP_CHOICES,
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
                       "error": "Не удалось сформировать документ (внутренняя ошибка). "
                                "Подробности — в логах web."})
    return _req_trigger()


# ── Корреспонденция: загрузка сканов (Входящие/Исходящие) ───────────────────

def _scan_to_storedfile(f, prefix="procedure/correspondence"):
    """Загрузить файл-скан в S3 → StoredFile (+ ссылка для предпросмотра)."""
    from apps.files.models import StoredFile
    from apps.files.s3_utils import upload_file_to_s3
    data = f.read()
    bucket, key = upload_file_to_s3(
        data, prefix=prefix, filename=f.name,
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

def _doc_paragraphs(req) -> list:
    """Редактируемые абзацы .docx документа запроса (пусто — документа нет).

    🛑 Тянет файл из S3 и парсит python-docx — вызывать только по явному
    действию юриста, не при каждом открытии карточки.
    """
    if not req.document_docx_id:
        return []
    from apps.files.s3_utils import download_file_from_s3

    from .request_documents import extract_editable_paragraphs
    data = download_file_from_s3(req.document_docx.bucket, req.document_docx.key)
    return extract_editable_paragraphs(data)


def _doc_paragraphs_safe(req):
    """(абзацы, текст ошибки) — файл может быть недоступен в S3 (чужой бакет,
    удалённый объект). Показываем это сообщением, а не 500-й."""
    try:
        return _doc_paragraphs(req), ""
    except Exception:
        import logging
        logging.getLogger(__name__).exception("не удалось прочитать .docx запроса %s", req.pk)
        return [], ("Не удалось открыть файл документа — он недоступен в хранилище. "
                    "Сформируйте документ заново или подгрузите готовый.")


def _posted_paragraphs(post) -> list:
    """Правки абзацев из формы (p_<индекс>) — в том же виде, что отдаёт парсер."""
    out = []
    for k, v in post.items():
        if not k.startswith("p_"):
            continue
        try:
            out.append({"index": int(k[2:]), "text": v})
        except ValueError:
            pass
    out.sort(key=lambda p: p["index"])
    return out


def _save_doc_text(req, post, employee):
    """Применить правку абзацев к .docx и пересобрать PDF (LibreOffice)."""
    from apps.files.s3_utils import download_file_from_s3

    from .request_documents import apply_paragraph_edits, save_edited_document
    data = download_file_from_s3(req.document_docx.bucket, req.document_docx.key)
    edits = {p["index"]: p["text"] for p in _posted_paragraphs(post)}
    save_edited_document(req, apply_paragraph_edits(data, edits), employee=employee)


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
    paras, error = _doc_paragraphs_safe(req)
    return render(request, "procedure/_request_edit_modal.html",
                  {"service": service, "req": req, "paras": paras, "error": error})


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
    try:
        _save_doc_text(req, request.POST, _actor(request))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("request_edit_save failed")
        return render(request, "procedure/_request_edit_modal.html", {
            "service": service, "req": req,
            "paras": _posted_paragraphs(request.POST),
            "error": "Не удалось сохранить (ошибка конвертации). Попробуйте ещё раз.",
        })
    return _req_trigger()


# ── Превью офисных файлов (doc/docx/xls…) = их PDF-рендер в iframe ────────────

@login_required
@require_procedures
def office_pdf(request, service_id, sf_id):
    """PDF-рендер офисного файла для предпросмотра в iframe (без внешнего вьюера).

    🛑 MS Office Online Viewer не годится: Microsoft скачивает файл со своих серверов,
    а наш S3 — Beget (российское облако `s3.ru1.storage.beget.cloud`), снаружи для них
    недоступен → вьюер ничего не открывает. Поэтому отдаём PDF сами:
    если у документа запроса уже есть PDF-двойник (`document_pdf`) — редирект на него;
    иначе конвертируем docx/xls на лету через LibreOffice.
    """
    from apps.files.models import StoredFile
    sf = get_object_or_404(StoredFile, pk=sf_id)
    rq = Request.objects.filter(document_docx=sf, document_pdf__isnull=False).first()
    if rq is not None:
        from django.shortcuts import redirect
        from django.urls import reverse
        return redirect(reverse("files:stored_download", args=[rq.document_pdf_id]) + "?inline=1")
    from apps.afd.pdf_utils import docx_to_pdf
    from apps.files.s3_utils import download_file_from_s3
    try:
        pdf = docx_to_pdf(download_file_from_s3(sf.bucket, sf.key))
    except Exception:
        return HttpResponse("Не удалось сконвертировать документ для предпросмотра", status=415)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = "inline; filename=preview.pdf"
    return resp


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
    from apps.afd.pdf_utils import pdf_page_count
    if is_docx:
        req.document_docx = sf
        try:
            from apps.afd.pdf_utils import docx_to_pdf
            pdf_bytes = docx_to_pdf(data)
            pdf_sf = _store(pdf_bytes, filename=f"{f.name[:-5]}.pdf", content_type="application/pdf")
            _attach(client, pdf_sf, emp)
            req.document_pdf = pdf_sf
            req.pages_count = pdf_page_count(pdf_bytes) or None
        except Exception:
            import logging
            logging.getLogger(__name__).exception("request_upload: docx→pdf failed")
    else:
        req.document_pdf = sf
        req.pages_count = pdf_page_count(data) or None
    req.generated_at = timezone.now()
    req.save(update_fields=[
        "document_docx", "document_pdf", "pages_count", "generated_at", "updated_at",
    ])
    return _req_trigger()


# ── Вкладка «Активы» ────────────────────────────────────────────────────────
# Загрузка справки ФНС (drag&drop / кнопка) → разбор в Celery с живым логом →
# «Сохранить» раскладывает распознанное по разделам (счета, 2-НДФЛ, имущество).

def _group_accounts(accounts):
    """Счета — группами по банкам (ключ — ИНН: отделения Сбера/ВТБ идут в одну
    группу, запрос выписки всё равно один на банк). Внутри группы — по номеру.
    Группы: сперва должник, потом супруг; далее по названию банка."""
    groups: dict[tuple, dict] = {}
    for acc in accounts:
        key = (acc.subject, acc.bank_inn or acc.bank_name)
        group = groups.setdefault(key, {
            "subject": acc.subject,
            "subject_display": acc.get_subject_display(),
            "bank_name": (acc.legal_entity.short_name or acc.legal_entity.name
                          if acc.legal_entity else acc.bank_name),
            "bank_inn": acc.bank_inn,
            "legal_entity": acc.legal_entity,
            "accounts": [],
        })
        group["accounts"].append(acc)
    out = sorted(groups.values(), key=lambda g: (g["subject"] != "debtor", g["bank_name"].lower()))
    for group in out:
        group["accounts"].sort(key=lambda a: a.number)
        group["open_count"] = sum(1 for a in group["accounts"] if a.is_open)
    return out


def _assets_context(case):
    from .models import BankAccount, IncomeCertificate, LandPlot, OtherAsset, RealEstateObject, Vehicle
    accounts = list(BankAccount.objects.filter(case=case)
                    .select_related("legal_entity", "statement_request")
                    .order_by("subject", "bank_name", "number"))
    return {
        "case": case,
        "service": case.service,
        "client": case.service.client,
        "accounts": accounts,
        "account_groups": _group_accounts(accounts),
        "incomes": IncomeCertificate.objects.filter(case=case).order_by("-year", "agent_name"),
        "realty": RealEstateObject.objects.filter(case=case).order_by("subject", "object_type"),
        "land": LandPlot.objects.filter(case=case).order_by("subject", "address"),
        "vehicles": Vehicle.objects.filter(case=case).order_by("subject", "model"),
        "others": OtherAsset.objects.filter(case=case).order_by("kind", "title"),
        "documents": (case.asset_documents.select_related("stored_file")
                      .order_by("-created_at")),
    }


@never_cache
@login_required
@require_procedures
def tab_assets(request, service_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    return render(request, "procedure/_tab_assets.html", _assets_context(case))


@login_required
@require_procedures
@require_POST
def assets_upload(request, service_id):
    """Приём файла: кладём в Redis, ставим разбор в Celery, отдаём модалку с логом."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)

    f = request.FILES.get("file")
    if not f:
        return HttpResponseBadRequest("Файл не передан")
    if not f.name.lower().endswith(".pdf"):
        return render(request, "procedure/_assets_parse_modal.html",
                      {"service": service, "error": "Пока распознаём только PDF-справки ФНС."})
    if f.size > 30 * 1024 * 1024:
        return render(request, "procedure/_assets_parse_modal.html",
                      {"service": service, "error": "Файл больше 30 МБ — это точно справка ФНС?"})

    from .tasks import FNS_TTL, fns_file_key, fns_job_key, parse_fns_document

    token = uuid.uuid4().hex
    cache.set(fns_file_key(token), f.read(), FNS_TTL)
    cache.set(fns_job_key(token), {
        "status": "running", "filename": f.name, "size": f.size, "log": [],
    }, FNS_TTL)
    parse_fns_document.delay(token, str(case.id))

    return render(request, "procedure/_assets_parse_modal.html",
                  {"service": service, "token": token, "filename": f.name})


@never_cache
@login_required
@require_procedures
def assets_parse_progress(request, service_id, token):
    """Лог разбора (поллинг из модалки, ~600 мс)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    from .tasks import fns_job

    job = fns_job(token) or {"status": "failed", "error": "Разбор не найден — загрузите файл заново."}
    return render(request, "procedure/_assets_parse_log.html",
                  {"service": service, "token": token, "job": job,
                   "done": job.get("status") in ("done", "failed")})


@login_required
@require_procedures
@require_POST
def assets_save(request, service_id, token):
    """«Сохранить» — разложить распознанное по моделям + подшить исходник в S3."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)

    from .assets import save_parsed
    from .tasks import fns_file_key, fns_job, fns_job_key

    job = fns_job(token)
    if not job or job.get("status") != "done" or not job.get("result"):
        return HttpResponseBadRequest("Результат разбора не найден — загрузите файл заново.")

    stored = None
    data = cache.get(fns_file_key(token))
    if data:
        from apps.files.models import StoredFile
        from apps.files.s3_utils import upload_file_to_s3
        bucket, key = upload_file_to_s3(
            data, prefix="procedure/assets", filename=job.get("filename", "fns.pdf"),
            content_type="application/pdf",
        )
        stored = StoredFile.objects.create(
            bucket=bucket, key=key, filename=job.get("filename", "fns.pdf"),
            content_type="application/pdf", size=len(data),
        )

    save_parsed(case, job["result"], stored_file=stored,
                filename=job.get("filename", ""), employee=_actor(request))

    cache.delete(fns_job_key(token))
    cache.delete(fns_file_key(token))
    return render(request, "procedure/_tab_assets.html", _assets_context(case))


@login_required
@require_procedures
@require_POST
def assets_cancel(request, service_id, token):
    """«Отмена» — выбросить незасохранённый разбор."""
    from .tasks import fns_file_key, fns_job_key
    cache.delete(fns_job_key(token))
    cache.delete(fns_file_key(token))
    return HttpResponse("")


@login_required
@require_procedures
@require_POST
def assets_document_delete(request, service_id, doc_id):
    """Удалить документ-источник вместе со всеми распознанными из него записями."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    from .models import AssetDocument
    get_object_or_404(AssetDocument, id=doc_id, case=case).delete()
    return render(request, "procedure/_tab_assets.html", _assets_context(case))


# ── Вложения: доп. файлы к запросу / письму корреспонденции ─────────────────
# Одна пара вью на обе таблицы — владелец адресуется парой (target, obj_id),
# где target ∈ {request, correspondence}. См. models.DocumentAttachment.

_ATTACH_PREFIX = "procedure/attachments"


def _attach_owner(case, target, obj_id):
    """Владелец вложений, проверенный на принадлежность делу (иначе 404/None)."""
    if target == "request":
        return get_object_or_404(Request, pk=obj_id, case=case)
    if target == "correspondence":
        return get_object_or_404(Correspondence, pk=obj_id, service=case.service)
    return None


# Офисные форматы показываем через office_pdf (рендер в PDF) — inline-просмотр
# .docx/.xls браузер не умеет, а MS Viewer до нашего S3 (Beget) не достучится.
_OFFICE_EXT = (".doc", ".docx", ".xls", ".xlsx", ".rtf", ".odt")


def _attachments_ctx(service, target, owner):
    items = list(owner.attachments.select_related("stored_file", "uploaded_by__user"))
    for a in items:
        fn = ((a.stored_file.filename if a.stored_file_id else "") or "").lower()
        a.is_office = fn.endswith(_OFFICE_EXT)
    return {"service": service, "target": target, "obj": owner, "attachments": items}


def _render_attachments(request, service, target, owner):
    """Блок вложений + сигнал обновить таблицу под модалкой (счётчик 📎)."""
    resp = render(request, "procedure/partials/_attachments_block.html",
                  _attachments_ctx(service, target, owner))
    resp["HX-Trigger"] = "reloadRequests"
    return resp


@never_cache
@login_required
@require_procedures
def attachments_block(request, service_id, target, obj_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    owner = _attach_owner(case, target, obj_id)
    if owner is None:
        return HttpResponseBadRequest("Неизвестный тип владельца вложений")
    return render(request, "procedure/partials/_attachments_block.html",
                  _attachments_ctx(service, target, owner))


@login_required
@require_procedures
@require_POST
def attachments_add(request, service_id, target, obj_id):
    """Загрузить один или несколько файлов и привязать их к запросу/письму."""
    from django.contrib.contenttypes.models import ContentType

    from .models import DocumentAttachment
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    owner = _attach_owner(case, target, obj_id)
    if owner is None:
        return HttpResponseBadRequest("Неизвестный тип владельца вложений")
    ct = ContentType.objects.get_for_model(owner.__class__)
    employee = _actor(request)
    for f in request.FILES.getlist("files"):
        sf = _scan_to_storedfile(f, prefix=_ATTACH_PREFIX)
        DocumentAttachment.objects.create(
            content_type=ct, object_id=owner.pk, stored_file=sf,
            name=(f.name or "")[:255], uploaded_by=employee,
        )
    return _render_attachments(request, service, target, owner)


@login_required
@require_procedures
@require_POST
def attachment_delete(request, service_id, target, obj_id, att_id):
    """Отвязать вложение. Сам объект в S3 не трогаем (как и в остальных разделах)."""
    from .models import DocumentAttachment
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    owner = _attach_owner(case, target, obj_id)
    if owner is None:
        return HttpResponseBadRequest("Неизвестный тип владельца вложений")
    DocumentAttachment.objects.filter(pk=att_id, object_id=owner.pk).delete()
    return _render_attachments(request, service, target, owner)


# ── Карточка запроса: редактирование всех параметров ────────────────────────

def _int_or_none(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@never_cache
@login_required
@require_procedures
def request_card(request, service_id, req_id):
    """Модалка редактирования запроса (клик по строке в таблице)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(
        Request.objects.select_related("recipient", "request_type"), pk=req_id, case=case)
    return render(request, "procedure/_request_card_modal.html", {
        "service": service, "req": req,
        "request_types": RequestType.objects.filter(is_active=True).order_by("order", "name"),
        "method_choices": Request.METHOD_CHOICES,
        "status_choices": Request.STATUS_CHOICES,
        # Блок текста включается в карточку через {% include %} и берёт ЕЁ контекст,
        # поэтому флаг редактора нужен и здесь, не только в _text_block_ctx.
        "collabora": settings.COLLABORA_ENABLED,
        **_attachments_ctx(service, "request", req),
    })


@login_required
@require_procedures
@require_POST
def request_card_save(request, service_id, req_id):
    """Сохранить правку запроса. Пустые поля очищают значение (это правка, не патч)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(
        Request.objects.select_related("recipient", "request_type"), pk=req_id, case=case)

    old_type = req.request_type
    rt = RequestType.objects.filter(pk=request.POST.get("request_type")).first()
    req.request_type = rt
    # Название — снапшот типа. Правим его сами только если юрист его не менял
    # (оно совпадало с названием прежнего типа), иначе уважаем ручной текст.
    title = (request.POST.get("title") or "").strip()
    if rt and old_type and rt != old_type and title == (old_type.name or ""):
        title = rt.name
    req.title = title or (rt.name if rt else req.title)

    rid = (request.POST.get("recipient_id") or "").strip()
    req.recipient = LegalEntity.objects.filter(pk=rid).first() if rid else None
    rec_name = (request.POST.get("recipient_name") or "").strip()
    if not rec_name and req.recipient:
        rec_name = req.recipient.short_name or req.recipient.name
    req.recipient_name = rec_name[:255]

    req.outgoing_number = _int_or_none(request.POST.get("outgoing_number"))
    status = (request.POST.get("status") or "").strip()
    if status in dict(Request.STATUS_CHOICES):
        req.status = status
    method = (request.POST.get("sent_method") or "").strip()
    req.sent_method = method if method in dict(Request.METHOD_CHOICES) else ""
    req.sent_date = _date(request.POST.get("sent_date"))
    req.response_days = _int_or_none(request.POST.get("response_days"))

    old_due = req.due_date
    req.due_date = _date(request.POST.get("due_date"))
    # Срок сдвинули — дать просрочке сработать заново (уведомление шлётся раз).
    if req.due_date != old_due:
        req.overdue_notified = False

    req.response_date = _date(request.POST.get("response_date"))
    req.response_number = (request.POST.get("response_number") or "").strip()[:120]
    req.response_text = (request.POST.get("response_text") or "").strip()
    req.notes = (request.POST.get("notes") or "").strip()
    req.save()
    return _req_trigger()


# ── Карточка письма корреспонденции (Входящие / Исходящие) ──────────────────

@never_cache
@login_required
@require_procedures
def correspondence_card(request, service_id, corr_id):
    """Модалка редактирования письма (клик по строке во «Входящих»/«Исходящих»)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    co = get_object_or_404(
        Correspondence.objects.select_related("counterparty", "stored_file", "request"),
        pk=corr_id, service=service)
    from apps.crm.models import LegalEntityKind
    return render(request, "procedure/_correspondence_card_modal.html", {
        "service": service, "co": co,
        "kinds": LegalEntityKind.objects.order_by("name"),
        "direction_choices": Correspondence.DIRECTION_CHOICES,
        "delivery_choices": Correspondence.DELIVERY_CHOICES,
        "case_requests": case.requests.select_related("recipient").order_by("outgoing_number"),
        **_attachments_ctx(service, "correspondence", co),
    })


@login_required
@require_procedures
@require_POST
def correspondence_card_save(request, service_id, corr_id):
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    services.ensure_case(service)
    co = get_object_or_404(Correspondence, pk=corr_id, service=service)

    direction = (request.POST.get("direction") or "").strip()
    if direction in dict(Correspondence.DIRECTION_CHOICES):
        co.direction = direction
    co.subject_type = (request.POST.get("subject_type") or "").strip()[:255]
    rid = (request.POST.get("recipient_id") or "").strip()
    if rid:
        co.counterparty = LegalEntity.objects.filter(pk=rid).first() or co.counterparty
    elif request.POST.get("clear_counterparty") in ("1", "on", "true"):
        co.counterparty = None
    co.outgoing_number = (request.POST.get("outgoing_number") or "").strip()[:100]
    co.sent_at = _date(request.POST.get("sent_at"))
    method = (request.POST.get("delivery_method") or "").strip()
    co.delivery_method = method if method in dict(Correspondence.DELIVERY_CHOICES) else ""

    co.track_response = bool(request.POST.get("track_response"))
    co.control_date = _date(request.POST.get("control_date"))
    co.response_received = bool(request.POST.get("response_received"))
    co.response_date = _date(request.POST.get("response_date"))
    co.response_number = (request.POST.get("response_number") or "").strip()[:100]
    co.response_text = (request.POST.get("response_text") or "").strip()
    co.comments = (request.POST.get("comments") or "").strip()

    req_id = (request.POST.get("request_id") or "").strip()
    co.request = (Request.objects.filter(pk=req_id, case__service=service).first()
                  if req_id else None)

    # Замена основного скана (доп. файлы — блок «Вложения», отдельным запросом).
    f = request.FILES.get("scan")
    if f:
        co.stored_file = _scan_to_storedfile(f)
        co.file_link = ""
    co.save()
    return _req_trigger()


# ── Текст документа запроса прямо в карточке (ленивая подгрузка) ────────────
# Отдельный блок, а не часть карточки: абзацы тянутся из S3 и парсятся
# python-docx — делать это на каждый клик по строке таблицы недопустимо.

def _text_block_ctx(service, req, *, editing=False, paras=None, error="",
                    saved=False, pdf_rebuilt=False):
    return {
        "service": service, "req": req, "editing": editing,
        "paras": paras if paras is not None else [],
        "error": error, "saved": saved, "pdf_rebuilt": pdf_rebuilt,
        # Кнопки полноценного редактора нет, пока на сервере не поднят Collabora.
        "collabora": settings.COLLABORA_ENABLED,
    }


@never_cache
@login_required
@require_procedures
def request_text_block(request, service_id, req_id):
    """Блок текста документа: развернуть редактор или свернуть обратно (?collapsed=1)."""
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    editing = request.GET.get("collapsed") not in ("1", "true")
    paras, error = _doc_paragraphs_safe(req) if editing else ([], "")
    return render(request, "procedure/partials/_request_text_block.html",
                  _text_block_ctx(service, req, editing=editing, paras=paras, error=error))


@login_required
@require_procedures
@require_POST
def request_pdf_rebuild(request, service_id, req_id):
    """Пересобрать PDF из текущего .docx — по кнопке, а не автоматом.

    🛑 Синхронно, как и формирование документа: LibreOffice отрабатывает
    секунды, а юристу нужен результат сразу — иначе пришлось бы городить
    поллинг статуса ради одной кнопки.
    """
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(
        Request.objects.select_related("document_docx", "document_pdf"),
        pk=req_id, case=case)
    from .request_documents import RequestDocError, rebuild_pdf_from_docx
    error, rebuilt = "", False
    try:
        rebuild_pdf_from_docx(req)
        rebuilt = True
    except RequestDocError as exc:
        error = str(exc)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("request_pdf_rebuild failed")
        error = ("Не удалось пересобрать PDF (ошибка конвертации). "
                 "Подробности — в логах web.")
    resp = render(request, "procedure/partials/_request_text_block.html",
                  _text_block_ctx(service, req, error=error, pdf_rebuilt=rebuilt))
    if rebuilt:
        resp["HX-Trigger"] = "reloadRequests"
    return resp


@login_required
@require_procedures
@require_POST
def request_text_save(request, service_id, req_id):
    """Сохранить правку текста: .docx → пересборка PDF → блок с результатом.

    В отличие от отдельной модалки правки, карточка остаётся открытой — поэтому
    отдаём перерисованный блок, а таблицу под модалкой обновляем HX-Trigger'ом.
    """
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(Request, pk=req_id, case=case)
    if not req.document_docx_id:
        return HttpResponseBadRequest("Нет документа для редактирования")
    try:
        _save_doc_text(req, request.POST, _actor(request))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("request_text_save failed")
        return render(request, "procedure/partials/_request_text_block.html",
                      _text_block_ctx(service, req, editing=True,
                                      paras=_posted_paragraphs(request.POST),
                                      error="Не удалось сохранить (ошибка конвертации). "
                                            "Попробуйте ещё раз."))
    req.refresh_from_db()
    paras, error = _doc_paragraphs_safe(req)
    resp = render(request, "procedure/partials/_request_text_block.html",
                  _text_block_ctx(service, req, editing=True, paras=paras,
                                  error=error, saved=True))
    resp["HX-Trigger"] = "reloadRequests"
    return resp
