"""Единый пакет запросов БФЛ: состав из 17 позиций + недостающие типы.

По указанию юриста (13.07.2026) выбор пакета из UI убран — пакет один.
Что делает миграция (идемпотентно):
  • заводит вид ЮЛ «УФССП» (региональные управления ФССП — не путать с
    районными ОСП, у которых вид «ФССП»);
  • создаёт 5 недостающих типов: ЛРР, инф. письмо в ФССП, инф. письмо в УФССП,
    уведомление должнику, уведомление кредиторам;
  • собирает пакет `pkg_main` из 17 позиций в нужном порядке;
  • удаляет старые «Базовый»/«Расширенный» пакеты.

Шаблоны .docx для новых типов подгружаются отдельно (второй шаг) — тип без
шаблона создаётся нормально, предпроверка генерации подсветит его отсутствие.
"""
from django.db import migrations

UFSSP_KIND = ("Управление ФССП России по субъекту РФ (региональное)", "УФССП")

# Недостающие типы: (code, name, kind_short|None, lookup, response_days, order)
NEW_TYPES = [
    ("req_lrr", "Запрос в ЛРР (оружие)", "ЛРР", "region", 30, 75),
    ("req_fssp_info", "Информационное письмо в ФССП (по месту жительства)",
     "ФССП", "region", 30, 100),
    ("req_ufssp_info", "Информационное письмо в УФССП (региональное управление)",
     "УФССП", "region", 30, 105),
    ("req_notice_debtor", "Уведомление должнику", None, "debtor", 30, 120),
    ("req_notice_creditors", "Уведомление кредиторам о праве предъявления требований",
     None, "creditors", 30, 130),
]

# Состав единого пакета — порядок как в ТЗ юриста.
PACKAGE_CODE = "pkg_main"
PACKAGE_NAME = "Пакет запросов БФЛ"
PACKAGE_TYPES = [
    "req_rosreestr",         # заглушка (СМЭВ, адресат не требуется)
    "req_gibdd",             # ГИБДД/МРЭО — транспорт
    "req_gims",              # ГИМС — маломерные суда
    "req_gostehnadzor",      # Гостехнадзор — самоходная техника
    "req_dmi",               # ДМИ — муниципальное имущество
    "req_fns",               # ФНС
    "req_fns_orgs",          # ИФНС
    "req_sfr",               # СФР/ПФР
    "req_zags",              # ЗАГС
    "req_lrr",               # ЛРР — оружие
    "req_court",             # районный суд
    "req_employment",        # центр занятости
    "req_bank",              # банки — движение денежных средств
    "req_fssp_info",         # инф. письмо в ФССП (по месту жительства)
    "req_ufssp_info",        # инф. письмо в УФССП (региональное управление)
    "req_notice_debtor",     # уведомление должнику
    "req_notice_creditors",  # уведомление кредиторам
]

OLD_PACKAGES = ["pkg_basic", "pkg_full"]


def forwards(apps, schema_editor):
    RequestType = apps.get_model("procedure", "RequestType")
    RequestPackage = apps.get_model("procedure", "RequestPackage")
    LegalEntityKind = apps.get_model("crm", "LegalEntityKind")

    kind_ufssp, _ = LegalEntityKind.objects.get_or_create(
        name=UFSSP_KIND[0], defaults={"short_name": UFSSP_KIND[1]},
    )
    kinds = {k.short_name: k for k in LegalEntityKind.objects.all()}
    kinds.setdefault(UFSSP_KIND[1], kind_ufssp)

    for code, name, kind_short, lookup, days, order in NEW_TYPES:
        RequestType.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "recipient_kind": kinds.get(kind_short) if kind_short else None,
                "recipient_lookup": lookup,
                "response_days": days,
                "order": order,
                "is_active": True,
                "is_draft": True,
            },
        )

    pkg, _ = RequestPackage.objects.update_or_create(
        code=PACKAGE_CODE,
        defaults={"name": PACKAGE_NAME, "order": 10,
                  "is_active": True, "is_draft": True},
    )
    types = {t.code: t for t in RequestType.objects.filter(code__in=PACKAGE_TYPES)}
    pkg.types.set([types[c] for c in PACKAGE_TYPES if c in types])

    RequestPackage.objects.filter(code__in=OLD_PACKAGES).delete()


def backwards(apps, schema_editor):
    RequestPackage = apps.get_model("procedure", "RequestPackage")
    RequestPackage.objects.filter(code=PACKAGE_CODE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("procedure", "0018_alter_requesttype_recipient_lookup"),
        ("crm", "0098_legalentity_postal_code"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
