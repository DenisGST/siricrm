"""
Сиды для финансовых справочников «в разрезе услуги БФЛ».

Если в системе нет ServiceName с short_name='БФЛ', сиды пропускаются
без ошибки (миграция остаётся идемпотентной).
"""
from django.db import migrations


EXPENSE_TYPES_BFL = [
    "Агентское вознаграждение",
    "Взнос СРО",
    "Возмещение затрат АУ",
    "Выплата кредитору",
    "Гос. пошлина",
    "Обслуживание счёта",
    "Почтовые расходы",
    "Прожиточный минимум",
    "Публикация ЕФРСБ",
    "Публикация Коммерсантъ",
    "Торговая площадка",
    "Иное",
]

INCOME_TYPES_BFL = [
    "Возмещение затрат",
    "Оплата с депозита суда",
    "Оплата юруслуг",
    "Реализация имущества",
    "Сбор документов",
    "Снятие со счёта должника",
    "Иное",
]

INCOMING_ACCOUNTS = [
    ("cash", "Основная касса"),
    ("bank", "Расчётный счёт"),
]

OUTGOING_ACCOUNTS = [
    ("cash", "Основная касса"),
    ("bank", "Расчётный счёт"),
]


def seed(apps, schema_editor):
    ServiceName = apps.get_model("crm", "ServiceName")
    ExpenseType = apps.get_model("finance", "ExpenseType")
    IncomeType = apps.get_model("finance", "IncomeType")
    IncomingAccount = apps.get_model("finance", "IncomingAccount")
    OutgoingAccount = apps.get_model("finance", "OutgoingAccount")

    bfl = ServiceName.objects.filter(short_name__iexact="БФЛ").first()
    if bfl:
        for name in EXPENSE_TYPES_BFL:
            ExpenseType.objects.get_or_create(
                service_name=bfl, name=name,
                defaults={"is_active": True},
            )
        for name in INCOME_TYPES_BFL:
            IncomeType.objects.get_or_create(
                service_name=bfl, name=name,
                defaults={"is_active": True},
            )

    for at, nm in INCOMING_ACCOUNTS:
        IncomingAccount.objects.get_or_create(
            account_type=at, name=nm, defaults={"is_active": True},
        )
    for at, nm in OUTGOING_ACCOUNTS:
        OutgoingAccount.objects.get_or_create(
            account_type=at, name=nm, defaults={"is_active": True},
        )


def unseed(apps, schema_editor):
    """Откат не удаляет сиды — справочники могут быть уже использованы."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0001_initial"),
        ("crm", "0051_remove_client_crm_client_is_iden_idx"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
