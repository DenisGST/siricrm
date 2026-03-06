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
