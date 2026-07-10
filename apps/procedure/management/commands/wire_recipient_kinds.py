"""Привязать типы запросов к видам госорганов (RequestType.recipient_kind).

Идемпотентно, без внешних вызовов. Нужна на ПРОДЕ после переноса справочников
СФР/Гостехнадзор (dev→prod dumpdata/loaddata): миграция 0017 проставила виды,
существовавшие на момент миграции, а СФР/Гостехнадзор появляются позже —
эта команда до-привязывает их (и заодно освежает остальные).
"""
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntityKind
from apps.procedure.models import RequestType

# code → (LegalEntityKind.short_name | None, recipient_lookup)
MAP = {
    "req_rosreestr":    (None,           "none"),
    "req_gibdd":        ("МРЭО",         "region"),
    "req_gostehnadzor": ("Гостехнадзор", "region"),
    "req_gims":         ("ГИМС",         "region"),
    "req_dmi":          ("ДМИ",          "region"),
    "req_fns":          ("ФНС",          "fns_by_address"),
    "req_fns_orgs":     ("ФНС",          "fns_by_address"),
    "req_sfr":          ("СФР",          "region"),
    "req_zags":         ("ЗАГС",         "region"),
    "req_bank":         ("Банк",         "manual"),
    "req_court":        ("Районный суд", "region"),
    "req_employment":   (None,           "region"),   # ЦЗН — импорт позже
    "req_info_gov":     (None,           "manual"),
    "req_other":        (None,           "manual"),
}


class Command(BaseCommand):
    help = "Привязать RequestType.recipient_kind/recipient_lookup по каталогу"

    def handle(self, *args, **opts):
        kinds = {k.short_name: k for k in LegalEntityKind.objects.all()}
        wired = 0
        for code, (kind_name, lookup) in MAP.items():
            rt = RequestType.objects.filter(code=code).first()
            if not rt:
                continue
            kind = kinds.get(kind_name) if kind_name else None
            rt.recipient_lookup = lookup
            rt.recipient_kind = kind
            rt.save(update_fields=["recipient_lookup", "recipient_kind"])
            wired += 1
            self.stdout.write(
                f"  {code:18} lookup={lookup:16} kind={kind.short_name if kind else '—'}")
        self.stdout.write(self.style.SUCCESS(f"Готово: {wired} типов."))
