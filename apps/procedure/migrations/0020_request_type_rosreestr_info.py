"""Отдельная позиция «Информационное письмо в Росреестр» (по указанию юриста).

В пакете уже есть «Запрос в Росреестр» — он идёт через СМЭВ и адресата не требует
(заглушка). Информационное письмо — другой документ: адресуется Управлению
Росреестра по субъекту (справочник заведён миграцией crm.0100), уведомляет о
признании должника банкротом и о последствиях по ст. 213.25 Закона о банкротстве.

Ставим сразу после запроса в Росреестр → в пакете становится 18 позиций.
"""
from django.db import migrations

CODE = "req_rosreestr_info"
NAME = "Информационное письмо в Росреестр"
KIND_SHORT = "Росреестр"
PACKAGE_CODE = "pkg_main"


def forwards(apps, schema_editor):
    RequestType = apps.get_model("procedure", "RequestType")
    RequestPackage = apps.get_model("procedure", "RequestPackage")
    LegalEntityKind = apps.get_model("crm", "LegalEntityKind")

    kind = LegalEntityKind.objects.filter(short_name=KIND_SHORT).first()
    rt, _ = RequestType.objects.update_or_create(
        code=CODE,
        defaults={
            "name": NAME,
            "recipient_kind": kind,
            "recipient_lookup": "region",   # одно управление на регион → подберётся само
            "response_days": 30,
            "order": 15,                    # сразу после req_rosreestr (order=10)
            "is_active": True,
            "is_draft": True,
        },
    )
    pkg = RequestPackage.objects.filter(code=PACKAGE_CODE).first()
    if pkg:
        pkg.types.add(rt)


def backwards(apps, schema_editor):
    RequestType = apps.get_model("procedure", "RequestType")
    RequestType.objects.filter(code=CODE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("procedure", "0019_seed_request_package_main"),
        ("crm", "0100_seed_rosreestr_regional"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
