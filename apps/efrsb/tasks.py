"""Celery-задачи мониторинга ЕФРСБ (дефолтная очередь — БЕЗ роутинга).

Read-API — чистый REST, отдельный раннер не нужен (в отличие от arbitr). Гейт —
config.monitor_enabled() (env). Throttle — EfrsbBankruptLink.next_sync_at. SETNX-лок
на дело. 429 → retry с backoff.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

from . import config, services
from .client import EfrsbError, EfrsbRateLimited
from .models import EfrsbBankruptLink

log = logging.getLogger(__name__)


@shared_task(name="efrsb.monitor_active_cases")
def monitor_active_cases() -> str:
    if not config.monitor_enabled():
        return "disabled"
    from apps.procedure.models import BankruptcyCase
    now = timezone.now()
    cases = (BankruptcyCase.objects.filter(status=BankruptcyCase.STATUS_ACTIVE)
             .select_related("efrsb_link"))
    dispatched = 0
    for case in cases:
        link = getattr(case, "efrsb_link", None)
        # Throttle: пропускаем, если синк недавно (next_sync_at в будущем).
        if link and link.next_sync_at and link.next_sync_at > now:
            continue
        # Без резолва должника и без ИНН/СНИЛС — поиск всё равно попробуем в one_case.
        monitor_one_case.delay(str(case.id))
        dispatched += 1
    return f"dispatched {dispatched}"


@shared_task(name="efrsb.monitor_one_case", bind=True, max_retries=5, default_retry_delay=30)
def monitor_one_case(self, case_id: str, *, download_files=None) -> str:
    if not config.monitor_enabled():
        return "disabled"
    from apps.procedure.models import BankruptcyCase
    case = BankruptcyCase.objects.filter(pk=case_id).select_related("service__client").first()
    if case is None:
        return "no_case"

    lock_key = f"efrsb:lock:{case_id}"
    if not cache.add(lock_key, "1", 600):
        return "locked"
    try:
        link = services.resolve_bankrupt_guid(case)
        if not link.bankrupt_guid:
            return "no_bankrupt_guid"
        dl = config.download_files_default() if download_files is None else download_files
        stats = services.sync_case(case, download_files=dl)
        return f"ok {stats}"
    except EfrsbRateLimited as exc:
        raise self.retry(exc=exc, countdown=60)
    except EfrsbError as exc:
        log.warning("efrsb monitor_one_case %s: %s", case_id, exc)
        EfrsbBankruptLink.objects.filter(case=case).update(last_error=str(exc)[:500])
        return "error"
    finally:
        cache.delete(lock_key)


@shared_task(name="efrsb.refresh_now")
def refresh_now(case_id: str) -> str:
    """Ручной прогон из UI («Обновить из ЕФРСБ сейчас») — игнорирует throttle."""
    if not config.is_configured():
        return "not configured"
    from apps.procedure.models import BankruptcyCase
    case = BankruptcyCase.objects.filter(pk=case_id).select_related("service__client").first()
    if case is None:
        return "no_case"
    lock_key = f"efrsb:lock:{case_id}"
    if not cache.add(lock_key, "1", 600):
        return "locked"
    try:
        link = services.resolve_bankrupt_guid(case)
        if not link.bankrupt_guid:
            return "no_bankrupt_guid"
        stats = services.sync_case(case, download_files=True)
        return f"ok {stats}"
    except EfrsbError as exc:
        log.warning("efrsb refresh_now %s: %s", case_id, exc)
        return "error"
    finally:
        cache.delete(lock_key)
