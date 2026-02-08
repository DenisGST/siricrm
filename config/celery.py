from celery import Celery
from celery.schedules import crontab
import os
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# Beat Schedule for periodic tasks
app.conf.beat_schedule = {
    'cleanup-old-logs-daily': {
        'task': 'apps.crm.tasks.cleanup_old_logs',
        'schedule': crontab(hour=2, minute=0),  # 2 AM daily
        'args': (30,)  # Delete logs older than 30 days
    },
    'generate-daily-report': {
        'task': 'apps.crm.tasks.generate_daily_report',
        'schedule': crontab(hour=22, minute=0),  # 10 PM daily
    },
    'sync-employee-status': {
        'task': 'apps.crm.tasks.sync_employee_status',
        'schedule': 60,  # Every minute
    },
}

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
