"""
Событие событийки «Иск отправлен в суд» (EventType claim_sent, notifies=True →
авто-уведомление всем закреплённым за клиентом). Создаётся кнопкой «Иск
отправлен в суд» в карточке услуги. Идемпотентно.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    EventType = apps.get_model("crm", "EventType")
    EventType.objects.update_or_create(
        code="claim_sent",
        defaults=dict(
            name="Иск отправлен в суд",
            source="court",
            order=100,
            is_system=True,
            is_manual=False,
            is_active=True,
            notifies=True,
            description="Заявление о банкротстве направлено в суд (дата и способ — в карточке дела).",
        ),
    )


def backwards(apps, schema_editor):
    apps.get_model("crm", "EventType").objects.filter(code="claim_sent").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0094_region_arbitr_code_data"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
