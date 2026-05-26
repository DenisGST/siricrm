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
