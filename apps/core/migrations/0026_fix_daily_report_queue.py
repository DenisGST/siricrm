"""Фикс к 0025: устанавливает PeriodicTask.queue='devops' для daily-health-report.

В первом сидере (0025) поле queue было не задано → default → None → beat шлёт
в очередь `default`, обрабатывает celery-1 worker. Тот без docker.sock
(сокет смонтирован только в devops-runner) → status-handler в отчёте видит
только web-контейнер вместо всех 13.

Без этой миграции после pull_db с прода queue снова станет None.

Идемпотентно: filter().update() — повторное применение не ломает.
"""
from django.db import migrations


def _fix_queue(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="daily-health-report").update(queue="devops")


def _revert_queue(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="daily-health-report").update(queue=None)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0025_seed_monitor_vpn_and_daily_report"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(_fix_queue, _revert_queue),
    ]
