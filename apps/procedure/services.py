"""Движок дела и процедур: ленивое создание дела, добавление процедур,
инстанцирование мероприятий по стадиям, пересчёт сроков по уровням,
смена стадии, фиксация исходов (с автозакрытием дела).

Чистые функции над ORM. Уведомления/лог — через `apps.crm.client_log`.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.crm import client_log

from .models import (
    CLOSING_OUTCOMES,
    SCOPE_COMMON,
    BankruptcyCase,
    Claim,
    Creditor,
    MilestoneTemplate,
    Procedure,
    ProcedureMilestone,
    ProcedureStage,
    Request,
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


# ── Запросы в госорганы ─────────────────────────────────────────────────────

def debtor_display(client) -> str:
    """ФИО должника — адресат уведомления должнику (не госорган, а сам клиент)."""
    return " ".join(filter(None, [
        client.last_name, client.first_name, client.patronymic,
    ])).strip() if client else ""


def case_creditors(service) -> list:
    """Кредиторы должника из анкеты БФЛ — адресаты уведомления о праве
    предъявления требований (по письму на каждого).

    Единый источник — тот же `isk_context.resolve_creditors`, что и в исковом
    (банки/МФО/маркетплейсы/коммуналка/суд/штрафы/прочее). Схлопываем дубли:
    два кредита в одном банке = один кредитор = одно письмо.
    Возвращает [(LegalEntity|None, name)] в порядке анкеты.
    """
    from apps.afd import isk_context
    from apps.crm.models import LegalEntity

    at = isk_context.answers_by_type(isk_context.latest_response(service))
    out, seen = [], set()
    for c in isk_context.resolve_creditors(at):
        le = (LegalEntity.objects.filter(pk=c["le_id"]).first()
              if c.get("le_id") else None)
        name = ((le.short_name or le.name) if le else (c.get("name") or "").strip())
        if not name or name == "—":
            continue
        key = str(le.pk) if le else name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((le, name))
    return out


@transaction.atomic
def create_request(case, request_type, *, recipient=None, employee=None,
                   recipient_name=None) -> Request:
    """Создать запрос по типу.

    Адресат: явно переданный → авто-подбор по типу+региону/адресу/правилам →
    госорган типа по умолчанию. Для уведомления должнику госоргана нет —
    в `recipient_name` пишем ФИО должника. Исходящий № присваивается сразу
    (сквозной по делу).
    """
    from .recipient_resolver import RequestTypeLookup, resolve_recipient

    rec = recipient
    if rec is None:
        res = resolve_recipient(request_type, case.service.client, case.service)
        rec = res["recipient"]
    rec = rec or request_type.default_recipient
    name = recipient_name
    if name is None:
        if rec:
            name = rec.short_name or rec.name
        elif request_type.recipient_lookup == RequestTypeLookup.DEBTOR:
            name = debtor_display(case.service.client)
        else:
            name = ""
    next_num = (case.requests.aggregate(m=Max("outgoing_number"))["m"] or 0) + 1
    return Request.objects.create(
        case=case,
        request_type=request_type,
        title=request_type.name,
        recipient=rec,
        recipient_name=name,
        response_days=request_type.response_days,
        outgoing_number=next_num,
        created_by=employee,
    )


@transaction.atomic
def create_creditor_notices(case, request_type, *, employee=None) -> list:
    """Уведомление кредиторам — по отдельному письму на каждого кредитора дела."""
    created = []
    for le, name in case_creditors(case.service):
        created.append(create_request(
            case, request_type, recipient=le, recipient_name=name, employee=employee))
    return created


@transaction.atomic
def create_requests_for_type(case, request_type, *, recipient=None, employee=None) -> list:
    """Создать запрос(ы) по типу.

    Обычный тип → один запрос. «Кредиторы» → по письму на каждого кредитора из
    анкеты БФЛ, НО только если адресат не выбран явно: кредитор может всплыть
    уже в ходе процедуры (заявился в реестр требований, нашёлся в ответе
    госоргана) — тогда юрист выбирает его из реестра юрлиц руками, и письмо
    создаётся одно, ему.
    """
    from .recipient_resolver import RequestTypeLookup
    if (request_type.recipient_lookup == RequestTypeLookup.CREDITORS
            and recipient is None):
        return create_creditor_notices(case, request_type, employee=employee)
    return [create_request(case, request_type, recipient=recipient, employee=employee)]


@transaction.atomic
def create_request_package(case, package, *, employee=None, recipients=None,
                           type_ids=None) -> list:
    """Создать запросы по типам пакета.

    `recipients` — dict {request_type_id: LegalEntity|None} с выбранными в модалке
    адресатами (перекрывают авто-подбор).
    `type_ids` — множество id отмеченных в модалке типов; None → все типы пакета.
    Тип «уведомление кредиторам» разворачивается в письмо на каждого кредитора.
    """
    recipients = recipients or {}
    created = []
    for rt in package.types.filter(is_active=True).order_by("order"):
        if type_ids is not None and str(rt.pk) not in type_ids:
            continue
        created.extend(create_requests_for_type(
            case, rt, recipient=recipients.get(str(rt.pk)), employee=employee))
    return created


def save_recipient_rule(request_type, client, service, recipient, *, employee=None):
    """Запомнить ручной выбор адресата для (вид ЮЛ + регион [+ район]).

    Переиспользуется резолвером у других клиентов того же региона/района.
    Возвращает RecipientRule или None (если нет вида/региона/адресата).
    """
    from .models import RecipientRule
    from .recipient_resolver import client_region, locality_key
    kind = request_type.recipient_kind if request_type else None
    if not (kind and recipient):
        return None
    region = client_region(client, service)
    if not region:
        return None
    # Для типов уровня региона правило пишем на весь регион (district=""),
    # иначе выбор запомнился бы только для района конкретного клиента.
    district = ("" if (getattr(request_type, "region_office_prefix", "") or "").strip()
                else locality_key(client))
    obj, _created = RecipientRule.objects.update_or_create(
        kind=kind, region=region, district=district,
        defaults={"recipient": recipient, "created_by": employee},
    )
    return obj


@transaction.atomic
def mark_request_sent(req: Request, *, method: str, sent_date, employee=None) -> Request:
    """Отметить отправку: способ + дата → пересчёт срока ответа, статус «Отправлен»."""
    req.sent_method = method or ""
    req.sent_date = sent_date
    days = req.response_days
    req.due_date = (sent_date + timedelta(days=days)) if (sent_date and days) else None
    req.status = Request.STATUS_SENT
    req.overdue_notified = False
    req.save(update_fields=[
        "sent_method", "sent_date", "due_date", "status", "overdue_notified", "updated_at",
    ])
    return req


@transaction.atomic
def set_request_response(req: Request, *, response_date=None, number="", text="",
                        no_answer=False, employee=None) -> Request:
    """Внести ответ (или пометить «Без ответа»)."""
    if no_answer:
        req.status = Request.STATUS_NO_ANSWER
    else:
        req.response_date = response_date
        req.response_number = number or ""
        req.response_text = text or ""
        req.status = Request.STATUS_ANSWERED
    req.save(update_fields=[
        "status", "response_date", "response_number", "response_text", "updated_at",
    ])
    return req


# ── Кредиторы и реестр требований (вкладка «Кредиторы / РТК») ───────────────

def next_creditor_number(case) -> int:
    """Следующий сквозной номер кредитора внутри дела."""
    return (case.creditors.aggregate(m=Max("number"))["m"] or 0) + 1


@transaction.atomic
def create_creditor(case, *, name: str, kind: str = Creditor.KIND_LEGAL,
                    legal_entity=None, number=None, employee=None,
                    source: str = Creditor.SOURCE_MANUAL, **fields) -> Creditor:
    """Завести кредитора. Номер присваивается сразу (как исх.№ у запроса)."""
    return Creditor.objects.create(
        case=case,
        name=(name or "").strip()[:500],
        kind=kind,
        legal_entity=legal_entity,
        number=number if number is not None else next_creditor_number(case),
        source=source,
        created_by=employee,
        **fields,
    )


def creditor_fields_from_legal_entity(le) -> dict:
    """Реквизиты кредитора из карточки юрлица реестра (адрес + банк + ИНН/ОГРН).

    🛑 Адрес для корреспонденции — почтовый, затем юридический, затем
    фактический: письма кредитору идут по почтовому, если он указан.
    """
    if le is None:
        return {}
    return {
        "inn": le.inn or "",
        "ogrn": le.ogrn or "",
        "corr_address": (le.postal_address or le.legal_address
                         or le.actual_address or ""),
        "bank_name": le.bank_name or "",
        "bik": le.bik or "",
        "settlement_account": le.settlement_account or "",
        "correspondent_account": le.correspondent_account or "",
    }


@transaction.atomic
def import_creditors_from_questionnaire(case, *, employee=None) -> dict:
    """Импорт кредиторов из анкеты БФЛ.

    Источник тот же, что у уведомления кредиторам (`case_creditors`) — банки,
    МФО, маркетплейсы, коммуналка, суд, штрафы, прочее. Идемпотентно: кредитор,
    уже заведённый по этому юрлицу (или с тем же наименованием), повторно не
    создаётся — так кнопку можно жать после каждого уточнения анкеты.

    Суммы берём из самой анкеты и складываем по кредитору: два кредита в одном
    банке = один кредитор с суммарным долгом. Требования (РТК) при импорте НЕ
    создаются — они появляются только после определения суда о включении.
    """
    from apps.afd import isk_context
    from apps.crm.models import LegalEntity

    at = isk_context.answers_by_type(isk_context.latest_response(case.service))
    # Схлопываем анкету по кредитору: ключ — юрлицо реестра либо имя.
    agg: dict = {}
    for c in isk_context.resolve_creditors(at):
        le = (LegalEntity.objects.filter(pk=c["le_id"]).first()
              if c.get("le_id") else None)
        name = ((le.short_name or le.name) if le else (c.get("name") or "").strip())
        if not name or name == "—":
            continue
        key = str(le.pk) if le else name.lower()
        row = agg.setdefault(key, {"le": le, "name": name, "amount": None})
        amt = c.get("amount")
        if amt is not None:
            row["amount"] = (row["amount"] or 0) + amt

    existing = list(case.creditors.all())
    have_le = {str(cr.legal_entity_id) for cr in existing if cr.legal_entity_id}
    have_name = {(cr.name or "").strip().lower() for cr in existing}

    created, skipped = [], 0
    for key, row in agg.items():
        le, name = row["le"], row["name"]
        if (le and str(le.pk) in have_le) or name.lower() in have_name:
            skipped += 1
            continue
        cr = create_creditor(
            case, name=name, kind=Creditor.KIND_LEGAL, legal_entity=le,
            employee=employee, source=Creditor.SOURCE_QUESTIONNAIRE,
            total_amount=row["amount"],
            **creditor_fields_from_legal_entity(le),
        )
        created.append(cr)
        if le:
            have_le.add(str(le.pk))
        have_name.add(name.lower())

    return {"created": created, "skipped": skipped, "total": len(agg)}


@transaction.atomic
def add_claim(creditor, *, queue: str, employee=None, **fields) -> Claim:
    """Добавить требование кредитору."""
    return Claim.objects.create(
        creditor=creditor, queue=queue, created_by=employee, **fields)


def creditors_summary(case) -> dict:
    """Свод по вкладке: сколько кредиторов/требований, суммы, сколько в реестре."""
    creditors = list(
        case.creditors.select_related("legal_entity").prefetch_related("claims"))
    claims = [c for cr in creditors for c in cr.claims.all()]
    included = [c for c in claims if c.registry_date]
    total_declared = sum(
        (cr.total_amount for cr in creditors if cr.total_amount is not None), Decimal("0"))
    total_included = sum((c.amount for c in included if c.amount is not None), Decimal("0"))
    return {
        "creditors": creditors,
        "count": len(creditors),
        "claims_count": len(claims),
        "included_count": len(included),
        "total_declared": total_declared,
        "total_included": total_included,
    }
