"""Фоновые задачи уведомлений."""
import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="notifications.revive_snoozed")
def revive_snoozed():
    """Вернуть отложенные уведомления в «Новые» по наступлении snooze_until.

    Beat будит раз в минуту. Находит snoozed с snooze_until <= now, переводит
    в new и пушит получателям обновление бейджа + маркер (звук/рефреш панели).
    """
    from .models import Notification
    from apps.realtime.utils import push_notification

    now = timezone.now()
    due = list(
        Notification.objects.filter(
            status=Notification.STATUS_SNOOZED,
            snooze_until__isnull=False,
            snooze_until__lte=now,
        ).select_related("recipient__user")
    )
    if not due:
        return 0

    ids = [n.pk for n in due]
    Notification.objects.filter(pk__in=ids).update(
        status=Notification.STATUS_NEW, snooze_until=None,
    )

    # Пуш по одному разу на получателя (бейдж считает общий счётчик).
    seen = set()
    for n in due:
        if n.recipient_id in seen:
            continue
        seen.add(n.recipient_id)
        n.status = Notification.STATUS_NEW  # для корректного рендера, если используется
        try:
            push_notification(n)
        except Exception:
            logger.exception("revive_snoozed: push упал для recipient=%s", n.recipient_id)

    logger.info("revive_snoozed: возвращено %d уведомлений в «Новые»", len(ids))
    return len(ids)
