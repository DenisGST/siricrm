"""Проставить способ определения адресата + вид госоргана типам запросов.

DRAFT-сид (правится в Справочниках). Виды Гостехнадзор/СФР/ЦЗН пока отсутствуют
в справочнике LegalEntityKind — им проставляем только lookup=region, вид
подключится после импорта справочников (Фаза 3).
"""
from django.db import migrations

# code → (LegalEntityKind.short_name | None, recipient_lookup)
MAP = {
    "req_rosreestr":   (None,            "none"),
    "req_gibdd":       ("МРЭО",          "region"),
    "req_gostehnadzor":(None,            "region"),   # вид появится после импорта
    "req_gims":        ("ГИМС",          "region"),
    "req_dmi":         ("ДМИ",           "region"),
    "req_fns":         ("ФНС",           "fns_by_address"),
    "req_fns_orgs":    ("ФНС",           "fns_by_address"),
    "req_sfr":         (None,            "region"),   # СФР/ПФР — после импорта
    "req_zags":        ("ЗАГС",          "region"),
    "req_bank":        ("Банк",          "manual"),
    "req_court":       ("Районный суд",   "region"),
    "req_employment":  (None,            "region"),   # ЦЗН — после импорта
    "req_info_gov":    (None,            "manual"),
    "req_other":       (None,            "manual"),
}


def forwards(apps, schema_editor):
    RequestType = apps.get_model("procedure", "RequestType")
    LegalEntityKind = apps.get_model("crm", "LegalEntityKind")
    kinds = {k.short_name: k for k in LegalEntityKind.objects.all()}
    for code, (kind_name, lookup) in MAP.items():
        rt = RequestType.objects.filter(code=code).first()
        if not rt:
            continue
        rt.recipient_lookup = lookup
        rt.recipient_kind = kinds.get(kind_name) if kind_name else None
        rt.save(update_fields=["recipient_lookup", "recipient_kind"])


def backwards(apps, schema_editor):
    RequestType = apps.get_model("procedure", "RequestType")
    RequestType.objects.update(recipient_lookup="manual", recipient_kind=None)


class Migration(migrations.Migration):
    dependencies = [
        ("procedure", "0016_requesttype_recipient_kind_and_more"),
        ("crm", "0097_backfill_ifns_code"),
    ]
    operations = [
        migrations.RunPython(forwards, backwards),
    ]
