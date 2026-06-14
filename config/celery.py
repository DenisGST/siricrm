#/var/www/projects/siricrm/config/celery.py
from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# Beat Schedule
app.conf.beat_schedule = {
    'cleanup-old-logs-daily': {
        'task': 'apps.crm.tasks.cleanup_old_logs',
        'schedule': crontab(hour=2, minute=0),
        'args': (30,)
    },
    'generate-daily-report': {
        'task': 'apps.crm.tasks.generate_daily_report',
        'schedule': crontab(hour=22, minute=0),
    },
    'sync-employee-status': {
        'task': 'apps.crm.tasks.sync_employee_status',
        'schedule': 60,
    },
    # Внешний мониторинг доступности другого окружения (dev↔prod).
    # No-op там, где не задан HEALTH_MONITOR_TARGET_URL.
    'monitor-health': {
        'task': 'apps.core.tasks.monitor_health',
        'schedule': 60,
    },
    # Telegram-бот мониторинга (кнопки): long-poll getUpdates. No-op, если
    # MONITOR_BOT_POLL=false (т.е. везде, кроме dev).
    'poll-monitor-bot': {
        'task': 'apps.core.tasks.poll_monitor_bot',
        'schedule': 15,
    },
    'mark-overdue-charges-daily': {
        'task': 'apps.finance.tasks.mark_overdue_charges',
        'schedule': crontab(hour=3, minute=0),
    },
    # Опрос Telegram'а вместо webhook (split-tunnel WireGuard не даёт
    # Telegram'у дотянуться до наших серверов).
    'poll-telegram-leads': {
        'task': 'telegram.poll_telegram_leads',
        'schedule': 10,
    },
    # Мониторинг арбитражных дел (kad.arbitr.ru). Сами таски имеют
    # внутреннюю проверку «работаем только 18:00–08:00», поэтому beat
    # просто будит их часто — оверхеда нет, реальная работа в окне.
    'arbitr-kad-monitor-pending': {
        'task': 'arbitr.kad_monitor_pending',
        'schedule': crontab(minute=0, hour='18-23,0-7'),
    },
    'arbitr-kad-monitor-case': {
        'task': 'arbitr.kad_monitor_case',
        'schedule': crontab(minute=30, hour='19,23,3,7'),
    },
    # Опрос выписки р/с ТБанк (входящие платежи → очередь разнесения).
    # Будим ежечасно; внутренний throttle (ACCOUNTING_POLL_MIN_INTERVAL_HOURS,
    # деф. 3ч) сам отсекает лишнее. No-op, если нет кредов / гейт выключен.
    'accounting-poll-statement': {
        'task': 'accounting.poll_statement',
        'schedule': crontab(minute=5),
    },
    # Возврат отложенных уведомлений в «Новые» по наступлении snooze_until.
    'notifications-revive-snoozed': {
        'task': 'notifications.revive_snoozed',
        'schedule': 60,
    },
}

@setup_logging.connect
def config_loggers(*args, **kwargs):
    """Настройка логирования Celery через Django settings"""
    from logging.config import dictConfig
    from django.conf import settings
    dictConfig(settings.LOGGING)

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
