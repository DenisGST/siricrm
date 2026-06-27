"""Сидим PeriodicTask для monitor_vpn (каждую минуту) и daily_health_report
(cron 8/13/19 МСК). На прод не повлияет — таски-получатели запускаются
только если у Celery beat прописано.

Идемпотентно: update_or_create по name. Откат удаляет PeriodicTask'и.

🛑 Включается ТОЛЬКО на dev — там, где задан MONITOR_BOT_POLL=true
(тот же сервер, где живёт monitor_health). На проде эти таски тоже создадутся
в БД (если миграция дойдёт), но beat-расписание на проде их не подхватит,
если PeriodicTask.enabled оставлен True вручную можно тоггнуть в админке.
"""
import json

from django.db import migrations


def _seed(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # monitor_vpn: каждую минуту
    interval, _ = IntervalSchedule.objects.get_or_create(every=60, period="seconds")
    PeriodicTask.objects.update_or_create(
        name="monitor-vpn",
        defaults={
            "task": "apps.core.tasks.monitor_vpn",
            "interval": interval,
            "crontab": None,
            "enabled": True,
            "description": "Проверка VPN-туннеля раз в минуту, алёрт в MAX+TG при 3 фейлах подряд",
        },
    )

    # daily_health_report: cron 8:00, 13:00, 19:00 МСК. CrontabSchedule хранит
    # время в TIME_ZONE проекта (Europe/Moscow), не в UTC — это default django-celery-beat.
    cron, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="8,13,19",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Europe/Moscow",
    )
    PeriodicTask.objects.update_or_create(
        name="daily-health-report",
        defaults={
            "task": "apps.core.tasks.daily_health_report",
            "crontab": cron,
            "interval": None,
            "enabled": True,
            "description": "Сводный отчёт о работоспособности 3 раза в день (08:00, 13:00, 19:00 МСК)",
        },
    )


def _unseed(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=["monitor-vpn", "daily-health-report"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0024_department_is_docs_collection"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(_seed, _unseed),
    ]
