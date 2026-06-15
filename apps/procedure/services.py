"""Движок дела и процедур: ленивое создание дела, добавление процедур,
инстанцирование мероприятий по стадиям, пересчёт сроков по уровням,
смена стадии, фиксация исходов (с автозакрытием дела).

Чистые функции над ORM. Уведомления/лог — через `apps.crm.client_log`.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.crm import client_log

from .models import (
    CLOSING_OUTCOMES,
    SCOPE_COMMON,
    BankruptcyCase,
    MilestoneTemplate,
    Procedure,
    ProcedureMilestone,
    ProcedureStage,
)


# ── Стадии ─────────────────────────────────────────────────────────────────

def _first_stage(scope: str) -> Optional[ProcedureStage]:
    return (
        ProcedureStage.objects.filter(kind_scope=scope, is_active=True, is_terminal=False)
        .order_by("order")
        .first()
    )


def _terminal_stage() -> Optional[ProcedureStage]:
    return (
        ProcedureStage.objects.filter(is_terminal=True, is_active=True)
        .order_by("order")
        .first()
    )


# ── Базовые даты ───────────────────────────────────────────────────────────

def _resolve_base_date(case: BankruptcyCase, ms: ProcedureMilestone) -> Optional[date]:
    """Значение базовой даты для мероприятия по его ключу.

    case_* — от полей дела; proc_* — от процедуры мероприятия (если есть).
    """
    key = ms.base_date_key
    if not key:
        return None
    if key == "case_filing_date":
        return case.filing_date
    if key == "case_claim_accept_date":
        return case.claim_accept_date
    if key == "case_first_hearing_date":
        return case.first_hearing_date
    proc = ms.procedure
    if proc is None:
        return None
    if key == "proc_intro_date":
        return proc.intro_date
    if key == "proc_publication_efrsb_date":
        return proc.publication_efrsb_date
    if key == "proc_publication_kommersant_date":
        return proc.publication_kommersant_date
    return None


# ── Дело ───────────────────────────────────────────────────────────────────

def _client_address(client, address_type: str) -> str:
    """Стандартизированный адрес клиента нужного типа (Address.result)."""
    addr = client.addresses.filter(address_type=address_type).first()
    return (addr.result or addr.source) if addr else ""


def _client_phones(client) -> str:
    """Телефоны клиента из ClientPhone (через запятую), иначе кэш Client.phone."""
    nums = list(client.phones.values_list("phone", flat=True))
    if nums:
        return ", ".join(dict.fromkeys(nums))  # уникальные, порядок сохранён
    return client.phone or ""


@transaction.atomic
def ensure_case(service) -> BankruptcyCase:
    """Получить/создать дело для услуги. Идемпотентно.

    На создании ставит первую общую стадию и инстанцирует её мероприятия.
    Ленивое создание — на первом открытии карточки.
    """
    case, created = BankruptcyCase.objects.get_or_create(service=service)
    if created:
        stage = _first_stage(SCOPE_COMMON)
        if stage is not None:
            case.current_stage = stage
            case.save(update_fields=["current_stage", "updated_at"])
            instantiate_stage_milestones(case, stage, procedure=None)
    return case


def instantiate_stage_milestones(
    case: BankruptcyCase, stage: ProcedureStage, procedure: Optional[Procedure] = None
) -> list[ProcedureMilestone]:
    """Создать экземпляры мероприятий по активным шаблонам стадии.

    Идемпотентно (UniqueConstraint). Уровень определяется procedure:
    None — общая фаза дела, иначе — конкретная процедура. Снапшотит поля.
    """
    if procedure is None:
        existing = set(
            case.milestones.filter(procedure__isnull=True, template__isnull=False)
            .values_list("template_id", flat=True)
        )
    else:
        existing = set(
            procedure.milestones.filter(template__isnull=False)
            .values_list("template_id", flat=True)
        )
    created: list[ProcedureMilestone] = []
    for tpl in MilestoneTemplate.objects.filter(stage=stage, is_active=True).order_by("order"):
        if tpl.id in existing:
            continue
        created.append(ProcedureMilestone.objects.create(
            case=case,
            procedure=procedure,
            template=tpl,
            stage=stage,
            title=tpl.title,
            base_date_key=tpl.base_date_key,
            offset_days=tpl.offset_days,
            is_mandatory=tpl.is_mandatory,
        ))
    if created:
        recompute_due_dates(case)
    return created


def recompute_due_dates(case: BankruptcyCase) -> int:
    """Пересчитать `due_date` у всех мероприятий дела из снапшота правила."""
    updated = 0
    qs = case.milestones.exclude(base_date_key="").select_related("procedure")
    for ms in qs:
        base = _resolve_base_date(case, ms)
        new_due = base + timedelta(days=ms.offset_days) if base else None
        if ms.due_date != new_due:
            ms.due_date = new_due
            ms.save(update_fields=["due_date", "updated_at"])
            updated += 1
    return updated


@transaction.atomic
def close_case(case: BankruptcyCase) -> BankruptcyCase:
    case.status = BankruptcyCase.STATUS_CLOSED
    term = _terminal_stage()
    if term is not None:
        case.current_stage = term
    case.save(update_fields=["status", "current_stage", "updated_at"])
    return case


@transaction.atomic
def reopen_case(case: BankruptcyCase) -> BankruptcyCase:
    """Снять закрытие (если исход поменяли). Возврат в активную процедуру/стадию."""
    case.status = BankruptcyCase.STATUS_ACTIVE
    if case.current_procedure_id and case.current_procedure.current_stage_id:
        case.current_stage = case.current_procedure.current_stage
    case.save(update_fields=["status", "current_stage", "updated_at"])
    return case


@transaction.atomic
def set_first_hearing_outcome(case: BankruptcyCase, code: str, *, employee=None) -> BankruptcyCase:
    case.first_hearing_outcome = code or ""
    case.save(update_fields=["first_hearing_outcome", "updated_at"])
    if code in CLOSING_OUTCOMES:
        close_case(case)
    elif case.status == BankruptcyCase.STATUS_CLOSED:
        reopen_case(case)
    return case


# ── Процедуры ──────────────────────────────────────────────────────────────

@transaction.atomic
def add_procedure(
    case: BankruptcyCase, kind: str, *, intro_date=None, employee=None
) -> Procedure:
    """Добавить процедуру в дело: создать запись, сделать активной, перейти на
    её первую стадию, инстанцировать её мероприятия, записать событийку."""
    next_order = (case.procedures.aggregate(m=Max("order"))["m"] or 0) + 1
    proc = Procedure.objects.create(
        case=case, kind=kind, order=next_order, intro_date=intro_date,
    )
    stage = _first_stage(kind)
    case.current_procedure = proc
    if stage is not None:
        proc.current_stage = stage
        proc.save(update_fields=["current_stage", "updated_at"])
        case.current_stage = stage
    if case.status == BankruptcyCase.STATUS_CLOSED:
        case.status = BankruptcyCase.STATUS_ACTIVE
    case.save(update_fields=["current_procedure", "current_stage", "status", "updated_at"])
    if stage is not None:
        instantiate_stage_milestones(case, stage, procedure=proc)
    recompute_due_dates(case)
    client_log.record_event(
        case.service.client, "procedure_added",
        comment=f"Добавлена процедура: {proc.get_kind_display()}",
        employee=employee, new_value=proc.get_kind_display(),
    )
    return proc


@transaction.atomic
def recompute_case_closed(case: BankruptcyCase) -> BankruptcyCase:
    """Пересчитать закрытость дела по терминальным исходам (1-го заседания
    или любой процедуры). Закрыть/переоткрыть при необходимости."""
    closing = (
        case.first_hearing_outcome in CLOSING_OUTCOMES
        or case.procedures.filter(outcome__in=CLOSING_OUTCOMES).exists()
    )
    if closing and case.status != BankruptcyCase.STATUS_CLOSED:
        close_case(case)
    elif not closing and case.status == BankruptcyCase.STATUS_CLOSED:
        reopen_case(case)
    return case


@transaction.atomic
def set_procedure_outcome(procedure: Procedure, code: str, *, employee=None) -> Procedure:
    procedure.outcome = code or ""
    procedure.save(update_fields=["outcome", "updated_at"])
    case = procedure.case
    if code in CLOSING_OUTCOMES:
        close_case(case)
    elif case.status == BankruptcyCase.STATUS_CLOSED:
        reopen_case(case)
    return procedure


# ── Смена стадии ───────────────────────────────────────────────────────────

@transaction.atomic
def enter_stage(
    case: BankruptcyCase, stage: ProcedureStage,
    procedure: Optional[Procedure] = None, *, employee=None,
) -> BankruptcyCase:
    """Перейти на стадию (общую — procedure=None, или процедуры)."""
    old = case.current_stage
    case.current_stage = stage
    if procedure is not None:
        case.current_procedure = procedure
        procedure.current_stage = stage
        procedure.save(update_fields=["current_stage", "updated_at"])
        case.save(update_fields=["current_stage", "current_procedure", "updated_at"])
    else:
        case.save(update_fields=["current_stage", "updated_at"])
    instantiate_stage_milestones(case, stage, procedure=procedure)
    client_log.record_event(
        case.service.client, "procedure_stage_changed",
        comment=f"Стадия: {stage.name}", employee=employee,
        old_value=old.name if old else "", new_value=stage.name,
    )
    return case


# ── Мероприятия ────────────────────────────────────────────────────────────

@transaction.atomic
def set_milestone_status(milestone: ProcedureMilestone, status: str, *, employee=None) -> ProcedureMilestone:
    milestone.status = status
    if status == ProcedureMilestone.STATUS_DONE:
        milestone.done_at = timezone.now()
        milestone.done_by = employee
    else:
        milestone.done_at = None
        milestone.done_by = None
    milestone.save(update_fields=["status", "done_at", "done_by", "updated_at"])
    return milestone


@transaction.atomic
def add_manual_milestone(
    case: BankruptcyCase, *, title: str, procedure: Optional[Procedure] = None,
    due_date=None, responsible=None, is_mandatory: bool = False, notes: str = "",
) -> ProcedureMilestone:
    stage = procedure.current_stage if procedure else case.current_stage
    return ProcedureMilestone.objects.create(
        case=case, procedure=procedure, template=None, stage=stage,
        title=title, due_date=due_date, responsible=responsible,
        is_mandatory=is_mandatory, is_manual=True, notes=notes,
    )
