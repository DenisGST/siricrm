"""Доменные функции интеграции ЕФРСБ (над ORM + client).

resolve_bankrupt_guid — резолв должника (ИНН/СНИЛС → bankruptGuid).
sync_case — выборка сообщений/отчётов по должнику → upsert публикаций.
upsert_publication / match_to_internal / apply_publication_date / flag_violation —
обработка одной публикации (дедуп по guid, привязка к нашей заготовке,
автозаполнение Procedure.publication_efrsb_date, флаг нарушения срока).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.crm import client_log

from . import client, config
from .models import EfrsbBankruptLink, EfrsbMessageType, EfrsbPublication

log = logging.getLogger(__name__)


# ── кэш сопоставления api_type → EfrsbMessageType ───────────────────────────

def _type_index() -> dict:
    """{api_type_lower: EfrsbMessageType} по всем активным типам (+алиасы)."""
    idx = {}
    for mt in EfrsbMessageType.objects.filter(is_active=True):
        for at in mt.all_api_types:
            idx.setdefault(at.lower(), mt)
    return idx


def _parse_dt(s):
    """ISO-строка ЕФРСБ (МСК, без tz) → aware datetime в текущем (Москва) tz."""
    if not s:
        return None
    raw = str(s).strip().replace(" ", "T", 1) if " " in str(s) and "T" not in str(s) else str(s)
    raw = raw.replace("Z", "")
    fmts = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
    from datetime import datetime as _dt
    for f in fmts:
        try:
            naive = _dt.strptime(raw[:26] if "." in raw else raw, f)
            return timezone.make_aware(naive, timezone.get_current_timezone())
        except ValueError:
            continue
    log.warning("efrsb: не распарсить datePublish=%r", s)
    return None


# ── резолв должника ──────────────────────────────────────────────────────────

def get_or_create_link(case) -> EfrsbBankruptLink:
    link, _ = EfrsbBankruptLink.objects.get_or_create(case=case)
    return link


def resolve_bankrupt_guid(case, *, force: bool = False) -> EfrsbBankruptLink:
    """Найти bankruptGuid должника по ИНН/СНИЛС. Несколько кандидатов → ручной выбор."""
    link = get_or_create_link(case)
    if link.bankrupt_guid and not force:
        return link
    client_obj = case.service.client
    inn = (client_obj.inn or "").strip()
    snils = (client_obj.snils or "").strip()
    if not inn and not snils:
        link.last_error = "У клиента не заполнены ИНН и СНИЛС — поиск невозможен."
        link.last_search_at = timezone.now()
        link.next_search_at = timezone.now() + timedelta(hours=config.search_retry_hours())
        link.save()
        return link

    page = []
    method = ""
    try:
        if inn:
            page = (client.search_bankrupts(type="Person", inn=inn).get("pageData") or [])
            method = EfrsbBankruptLink.MATCH_INN
        if not page and snils:
            page = (client.search_bankrupts(type="Person", snils=snils).get("pageData") or [])
            method = EfrsbBankruptLink.MATCH_SNILS
    except client.EfrsbError as e:
        link.last_error = str(e)[:500]
        link.last_search_at = timezone.now()
        link.next_search_at = timezone.now() + timedelta(hours=config.search_retry_hours())
        link.save()
        return link

    link.last_search_at = timezone.now()
    link.last_error = ""
    if len(page) == 1:
        link.bankrupt_guid = page[0].get("guid") or ""
        link.match_method = method
        link.match_confidence = EfrsbBankruptLink.CONF_AUTO
        link.candidates = []
        link.resolved_at = timezone.now()
        link.next_search_at = None
    elif len(page) > 1:
        link.candidates = page  # ручной выбор в UI
        link.match_method = method
        link.next_search_at = None
    else:
        link.candidates = []
        link.next_search_at = timezone.now() + timedelta(hours=config.search_retry_hours())
    link.save()
    return link


def confirm_bankrupt(case, bankrupt_guid: str) -> EfrsbBankruptLink:
    """Подтвердить должника вручную (из кандидатов)."""
    link = get_or_create_link(case)
    link.bankrupt_guid = (bankrupt_guid or "").strip()
    link.match_method = EfrsbBankruptLink.MATCH_MANUAL
    link.match_confidence = EfrsbBankruptLink.CONF_CONFIRMED
    link.candidates = []
    link.resolved_at = timezone.now()
    link.next_search_at = None
    link.save()
    return link


# ── обработка публикаций ─────────────────────────────────────────────────────

def upsert_publication(case, item: dict, *, kind: str, type_index=None) -> tuple[EfrsbPublication, bool]:
    """Идемпотентный апсёрт обнаруженной публикации по fedresurs_guid."""
    type_index = type_index if type_index is not None else _type_index()
    guid = (item.get("guid") or "").strip()
    if not guid:
        raise ValueError("upsert_publication: пустой guid")
    api_type = (item.get("type") or "").strip()
    mt = type_index.get(api_type.lower())

    pub, created = EfrsbPublication.objects.get_or_create(
        fedresurs_guid=guid,
        defaults={
            "case": case, "kind": kind,
            "origin": EfrsbPublication.ORIGIN_DISCOVERED,
            "status": EfrsbPublication.STATUS_PUBLISHED,
            "message_type": mt,
        },
    )
    # Обновляем поля факта публикации (на случай повторного синка с изменениями).
    pub.case = case
    pub.kind = kind
    if mt and pub.message_type_id is None:
        pub.message_type = mt
    pub.api_type = api_type
    pub.fedresurs_number = item.get("number") or ""
    pub.bankrupt_guid = item.get("bankruptGuid") or ""
    pub.date_publish = _parse_dt(item.get("datePublish"))
    pub.procedure_type = item.get("procedureType") or ""
    hv = item.get("hasViolation")
    pub.has_violation = hv if isinstance(hv, bool) else None
    annul = item.get("annulmentMessageGuid") or ""
    pub.annulment_guid = annul
    pub.is_annulled = bool(annul)
    lock = item.get("lockReason") or ""
    pub.lock_reason = lock
    pub.is_locked = bool(lock)
    if item.get("content"):
        pub.content_xml = item["content"]
    pub.raw = item
    if not pub.title:
        pub.title = (mt.name if mt else api_type) or "Публикация ЕФРСБ"
    if pub.is_annulled and pub.status not in (EfrsbPublication.STATUS_ANNULLED,):
        pub.status = EfrsbPublication.STATUS_ANNULLED
    elif pub.is_locked:
        pub.status = EfrsbPublication.STATUS_LOCKED
    if created:
        pub.discovered_at = timezone.now()
    pub.save()
    return pub, created


def match_to_internal(pub: EfrsbPublication) -> bool:
    """Привязать обнаруженную публикацию к нашей заготовке того же типа/процедуры
    без guid (превращаем 2 строки в 1). True — если привязали."""
    if pub.origin != EfrsbPublication.ORIGIN_DISCOVERED or not pub.message_type_id:
        return False
    from django.db.models import Q
    qs = EfrsbPublication.objects.filter(
        case=pub.case, origin=EfrsbPublication.ORIGIN_INTERNAL,
        message_type=pub.message_type, fedresurs_guid="",
    )
    if pub.procedure_id:
        qs = qs.filter(Q(procedure=pub.procedure) | Q(procedure__isnull=True))
    draft = qs.order_by("created_at").first()
    if draft is None:
        return False
    # Переносим факт публикации в нашу заготовку, обнаруженную строку удаляем.
    draft.fedresurs_guid = pub.fedresurs_guid
    draft.fedresurs_number = pub.fedresurs_number
    draft.bankrupt_guid = pub.bankrupt_guid
    draft.date_publish = pub.date_publish
    draft.api_type = pub.api_type
    draft.procedure_type = pub.procedure_type
    draft.has_violation = pub.has_violation
    draft.is_annulled = pub.is_annulled
    draft.annulment_guid = pub.annulment_guid
    draft.is_locked = pub.is_locked
    draft.lock_reason = pub.lock_reason
    draft.content_xml = pub.content_xml or draft.content_xml
    draft.raw = pub.raw
    draft.discovered_at = pub.discovered_at
    draft.matched_at = timezone.now()
    draft.status = pub.status if pub.status in (
        EfrsbPublication.STATUS_ANNULLED, EfrsbPublication.STATUS_LOCKED
    ) else EfrsbPublication.STATUS_PUBLISHED
    if pub.kind == EfrsbPublication.KIND_REPORT:
        draft.kind = EfrsbPublication.KIND_REPORT
    draft.save()
    pub.delete()
    return True


def apply_publication_date(pub: EfrsbPublication) -> bool:
    """«Вводное» сообщение → проставить Procedure.publication_efrsb_date (если пусто)
    и пересчитать дедлайны мероприятий. Идемпотентно. True — если проставили.

    🛑 Какие типы «вводные» — данные (EfrsbMessageType.sets_efrsb_date), TO CONFIRM.
    """
    mt = pub.message_type
    if not (mt and mt.sets_efrsb_date) or pub.is_annulled or not pub.date_publish:
        return False
    proc = pub.procedure or pub.case.current_procedure
    if proc is None or proc.publication_efrsb_date:
        return False
    proc.publication_efrsb_date = timezone.localtime(pub.date_publish).date()
    proc.save(update_fields=["publication_efrsb_date", "updated_at"])
    try:
        from apps.procedure.services import recompute_due_dates
        recompute_due_dates(pub.case)
    except Exception:
        log.exception("apply_publication_date: recompute_due_dates упал")
    return True


def flag_violation(pub: EfrsbPublication) -> bool:
    """hasViolation → разовая событийка efrsb_violation. True — если записали."""
    if not pub.has_violation:
        return False
    # Анти-дубль: пишем только при первом обнаружении (raw-флаг в notes-маркере).
    marker = "[violation_notified]"
    if marker in (pub.notes or ""):
        return False
    client_obj = pub.case.service.client
    try:
        client_log.invalidate_cache()
        client_log.record_event(
            client_obj, "efrsb_violation",
            comment=f"Публикация ЕФРСБ «{pub.type_label}» "
                    f"(№ {pub.fedresurs_number or '—'}) опубликована с нарушением срока.",
        )
        pub.notes = (pub.notes + " " + marker).strip()
        pub.save(update_fields=["notes", "updated_at"])
        return True
    except Exception:
        log.exception("flag_violation: не удалось записать событийку")
        return False


def sync_case(case, *, days: int = 31, download_files: bool = False) -> dict:
    """Выборка сообщений+отчётов по должнику дела → upsert/match/apply/flag.

    Возвращает статистику. Требует резолвленного bankrupt_guid.
    """
    link = get_or_create_link(case)
    if not link.bankrupt_guid:
        return {"skipped": "no_bankrupt_guid"}

    type_index = _type_index()
    date_begin = timezone.now() - timedelta(days=days)
    date_end = timezone.now()
    stats = {"new": 0, "updated": 0, "matched": 0, "date_applied": 0, "violations": 0}

    def _process(items, kind):
        for item in items:
            try:
                pub, created = upsert_publication(case, item, kind=kind, type_index=type_index)
            except Exception:
                log.exception("sync_case: upsert упал для item=%s", item.get("guid"))
                continue
            stats["new" if created else "updated"] += 1
            if created and match_to_internal(pub):
                stats["matched"] += 1
                pub = EfrsbPublication.objects.filter(fedresurs_guid=pub.fedresurs_guid).first()
            if pub is None:
                continue
            if apply_publication_date(pub):
                stats["date_applied"] += 1
            if flag_violation(pub):
                stats["violations"] += 1
            if download_files and created and not (pub.is_locked or pub.is_annulled):
                try:
                    from .files import download_publication_files
                    download_publication_files(pub)
                except Exception:
                    log.exception("sync_case: скачивание файлов упало для %s", pub.fedresurs_guid)

    try:
        msgs = list(client.iter_all(
            client.get_messages, bankrupt_guid=link.bankrupt_guid,
            date_begin=date_begin, date_end=date_end))
        _process(msgs, EfrsbPublication.KIND_MESSAGE)
        reports = list(client.iter_all(
            client.get_reports, bankrupt_guid=link.bankrupt_guid,
            date_begin=date_begin, date_end=date_end))
        _process(reports, EfrsbPublication.KIND_REPORT)
    except client.EfrsbRateLimited:
        raise
    except client.EfrsbError as e:
        link.last_error = str(e)[:500]
        link.save(update_fields=["last_error", "updated_at"])
        return {"error": str(e)}

    link.last_sync_at = timezone.now()
    link.next_sync_at = timezone.now() + timedelta(hours=config.sync_interval_hours())
    link.last_error = ""
    link.save()
    return stats
