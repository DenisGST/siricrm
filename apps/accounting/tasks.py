"""Celery-задачи поллинга источников входящих платежей.

Оркестрация рабочая уже в фазе A: throttle «не чаще раз в N часов», SETNX-лок,
дедуп по (source, external_id), журнал `SourcePoll`. Реальный HTTP-фетч —
`integrations.fetch_incoming` (фаза B). Без кредов источник = «не настроен».
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import integrations
from .models import IncomingPayment, SourcePoll

log = logging.getLogger(__name__)


def _poll(source: str, enabled: bool, force: bool = False) -> str:
    if not enabled or not integrations.is_configured(source):
        SourcePoll.objects.create(
            source=source, ok=False,
            error="Источник не настроен (нет кредов в env или поллинг выключен)",
        )
        return "not configured"

    interval = timedelta(hours=settings.ACCOUNTING_POLL_MIN_INTERVAL_HOURS)
    last_ok = SourcePoll.objects.filter(source=source, ok=True).order_by("-created_at").first()
    if not force and last_ok and last_ok.created_at > timezone.now() - interval:
        return "throttled"

    lock_key = f"acct:poll:{source}"
    if not cache.add(lock_key, "1", 600):
        return "locked"
    try:
        # Перехлёст 2 суток: платёж может появиться в выписке с задержкой;
        # дедуп по (source, external_id) делает повторную выборку безопасной.
        if last_ok:
            since = last_ok.created_at - timedelta(days=2)
        else:
            since = timezone.now() - timedelta(days=7)
        ops = integrations.fetch_incoming(source, since)
        created = 0
        for op in ops:
            _, was_created = IncomingPayment.objects.get_or_create(
                source=source,
                external_id=op["external_id"],
                defaults={
                    "occurred_at": op["occurred_at"],
                    "amount": op["amount"],
                    "payer_name": op.get("payer_name", ""),
                    "payer_inn": op.get("payer_inn", ""),
                    "payer_phone": op.get("payer_phone", ""),
                    "purpose": op.get("purpose", ""),
                    "order_id": op.get("order_id", ""),
                    "is_settlement": op.get("is_settlement", False),
                    "raw": op.get("raw", {}),
                },
            )
            if was_created:
                created += 1
        SourcePoll.objects.create(source=source, ok=True, found=len(ops), created=created)
        return f"ok +{created}"
    except Exception as exc:  # noqa: BLE001
        log.exception("Поллинг источника %s упал", source)
        SourcePoll.objects.create(source=source, ok=False, error=str(exc)[:500])
        return "error"
    finally:
        cache.delete(lock_key)


@shared_task(name="accounting.poll_statement")
def poll_statement(force: bool = False) -> str:
    return _poll(IncomingPayment.SOURCE_STATEMENT, settings.ACCOUNTING_STATEMENT_POLL_ENABLED, force)


@shared_task(name="accounting.poll_acquiring")
def poll_acquiring(force: bool = False) -> str:
    return _poll(IncomingPayment.SOURCE_ACQUIRING, settings.ACCOUNTING_ACQUIRING_POLL_ENABLED, force)


@shared_task(name="accounting.poll_incoming_source")
def poll_incoming_source(source: str, force: bool = False) -> str:
    """Ручной запуск из UI («Проверить сейчас»)."""
    if source == IncomingPayment.SOURCE_ACQUIRING:
        return poll_acquiring(force=force)
    return poll_statement(force=force)
