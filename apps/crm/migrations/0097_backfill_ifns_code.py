"""Бэкфилл LegalEntity.ifns_code из notes («Код ИФНС: XXXX») для ФНС.

Раньше код ИФНС клался только в notes. Теперь он в структурном поле —
переносим у существующих записей, чтобы работал автоподбор инспекции по адресу.
"""
import re

from django.db import migrations

_RE = re.compile(r"Код\s*ИФНС\s*[:\-]?\s*(\d{3,4})")


def forwards(apps, schema_editor):
    LegalEntity = apps.get_model("crm", "LegalEntity")
    LegalEntityKind = apps.get_model("crm", "LegalEntityKind")
    kind = LegalEntityKind.objects.filter(short_name="ФНС").first()
    if not kind:
        return
    qs = LegalEntity.objects.filter(kind=kind).exclude(notes="")
    bulk = []
    for le in qs.iterator():
        if le.ifns_code:
            continue
        m = _RE.search(le.notes or "")
        if not m:
            continue
        le.ifns_code = m.group(1)[:4]
        bulk.append(le)
        if len(bulk) >= 500:
            LegalEntity.objects.bulk_update(bulk, ["ifns_code"])
            bulk = []
    if bulk:
        LegalEntity.objects.bulk_update(bulk, ["ifns_code"])


def backwards(apps, schema_editor):
    # Необратимо в узком смысле не нужно чистить — оставляем как есть.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0096_legalentity_ifns_code"),
    ]
    operations = [
        migrations.RunPython(forwards, backwards),
    ]
