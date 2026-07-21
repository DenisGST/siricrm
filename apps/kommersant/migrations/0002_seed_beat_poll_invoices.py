"""Сидим PeriodicTask для приёма счетов из «Коммерсанта» (kommersant.poll_invoices).

Каждые 15 минут: заявок в день единицы, а ИД выставляет счёт не мгновенно —
чаще опрашивать IMAP смысла нет. Сама задача дёшева вхолостую: если нет заявок
в статусе «отправлена», она выходит сразу и к почте не подключается.

Идемпотентно: update_or_create по name. Откат удаляет PeriodicTask.
"""
from django.db import migrations

TASK_NAME = "kommersant-poll-invoices"


def _seed(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    interval, _ = IntervalSchedule.objects.get_or_create(every=15, period="minutes")
    PeriodicTask.objects.update_or_create(
        name=TASK_NAME,
        defaults={
            "task": "kommersant.poll_invoices",
            "interval": interval,
            "crontab": None,
            "enabled": True,
            "description": "Забирает счета ИД «Коммерсантъ» из ящиков АУ по отправленным заявкам",
        },
    )


def _unseed(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("kommersant", "0001_initial"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(_seed, _unseed)]
