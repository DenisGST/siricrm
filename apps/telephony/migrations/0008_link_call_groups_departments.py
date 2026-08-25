"""Связать группы входящих с отделами CRM.

Отделы в миграции `0007` намеренно не проставлялись — я не знал названий на
проде и не хотел угадыванием увести уведомления не тем людям. Названия
сверены с боевой базой (выгрузка `core.Department` через DevOps-агента
25.08.2026), совпадают с dev:

    Отдел продаж БФЛ · Юридический отдел БФЛ · Отдел сбора документов БФЛ ·
    Руководство · Бухгалтерия · IT отдел · Агенты

🛑 Матчим по ТОЧНОМУ имени и молча пропускаем ненайденное: на другой базе
(новый dev, тестовый стенд) названия могут отличаться, и подставлять «похожий»
отдел нельзя — уведомления о клиентах ушли бы посторонним. Если отдел не
проставился, уведомления всё равно дойдут: получателями остаются владельцы
внутренних номеров группы, подписчики и — как крайний случай — руководство.

🛑 `cc` → «Отдел продаж БФЛ» осознанно: отдельного отдела колл-центра в CRM
нет, а по Билайну обзвон КЦ вторым шагом эскалируется как раз на 301+302 —
то есть на продажи. Непринятый входящий = потерянный лид, и это их забота.
Если появится свой отдел КЦ — переставить в админке, кода это не касается.
"""
from django.db import migrations

LINKS = [
    ("cc",   "Отдел продаж БФЛ"),
    ("osd",  "Отдел сбора документов БФЛ"),
    ("yuro", "Юридический отдел БФЛ"),
]


def link(apps, schema_editor):
    CallGroup = apps.get_model("telephony", "CallGroup")
    Department = apps.get_model("core", "Department")
    for code, dep_name in LINKS:
        group = CallGroup.objects.filter(code=code).first()
        if group is None or group.department_id:
            continue                      # уже настроено руками — не трогаем
        dep = Department.objects.filter(name=dep_name).first()
        if dep is None:
            continue                      # другой стенд — оставляем пустым
        group.department = dep
        group.notify_department = True
        group.save(update_fields=["department", "notify_department"])


def unlink(apps, schema_editor):
    CallGroup = apps.get_model("telephony", "CallGroup")
    CallGroup.objects.filter(code__in=[c for c, _ in LINKS]).update(department=None)


class Migration(migrations.Migration):

    dependencies = [
        ("telephony", "0007_seed_call_groups"),
        ("core", "0039_employee_notify_missed_calls"),
    ]

    operations = [migrations.RunPython(link, unlink)]
