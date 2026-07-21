"""Сид каталога публикаций в «Коммерсантъ»: типы сообщений + типы лога.

Идемпотентен. По умолчанию НЕ затирает правки АУ (шаблоны текста, is_bfl-разметку) —
перезалить формулировки из кода можно флагом --force-templates.

🛑 Как и procedure_seed / efrsb_seed, в deploy-handler НЕ входит — гонять руками
   при выкатке: `python manage.py kommersant_seed`.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.crm.models import ActionType
from apps.kommersant.models import (
    KIND_REALIZATION,
    KIND_RESTRUCTURING,
    KommersantMessageType,
)

# 🛑 Формулировки — DRAFT, подтверждать с АУ (правятся в Справочниках).
# Сокращения намеренно раскрыты («года рождения», «электронная почта»): п. 4 Порядка,
# утв. Приказом Минэкономразвития РФ от 12.07.2010 № 292, запрещает сокращения в
# публикуемых сведениях, кроме предусмотренных НПА. ИД вправе снять такое сообщение.
_TEXT_REALIZATION = (
    # 🛑 После «Решением» суд идёт в родительном падеже — берём ключ
    # {арбитражного суда}, а не {арбитражный суд} (иначе «Решением Арбитражный суд»).
    "Решением {арбитражного суда} от {дата решения} по делу № {номер дела} "
    "{ФИО должника} ({дата рождения} года рождения, место рождения: {место рождения}, "
    "адрес регистрации: {адрес регистрации}, ИНН {ИНН}, СНИЛС {СНИЛС}) признан "
    "несостоятельным (банкротом), в отношении него введена процедура реализации "
    "имущества гражданина сроком на {срок процедуры} месяцев. "
    "Финансовым управляющим {утверждён ФУ} {ФИО Финансовый управляющий} "
    "(ИНН {ИНН АУ}, СНИЛС {СНИЛС АУ}, адрес для направления корреспонденции: "
    "{Адрес арбитражного управляющего}, электронная почта: {email арбитражного}), "
    "член {СРО полностью}. "
    "Требования кредиторов предъявляются в течение двух месяцев с даты опубликования "
    "настоящего сообщения."
)

_TEXT_RESTRUCTURING = (
    # 🛑 ФИО подставляется в именительном падеже и не склоняется, поэтому фраза
    # построена через тире: «...заявление о признании банкротом должника — Иванов И. И.».
    # Обороты вида «о признании гражданина {ФИО}» дают «гражданина Иванов Иван».
    "Определением {арбитражного суда} от {дата решения} по делу № {номер дела} "
    "признано обоснованным заявление о признании банкротом должника — "
    "{ФИО должника} ({дата рождения} года рождения, место рождения: {место рождения}, "
    "адрес регистрации: {адрес регистрации}, ИНН {ИНН}, СНИЛС {СНИЛС}); "
    "в отношении должника введена процедура реструктуризации долгов гражданина. "
    "Финансовым управляющим {утверждён ФУ} {ФИО Финансовый управляющий} "
    "(ИНН {ИНН АУ}, СНИЛС {СНИЛС АУ}, адрес для направления корреспонденции: "
    "{Адрес арбитражного управляющего}, электронная почта: {email арбитражного}), "
    "член {СРО полностью}. "
    "Требования кредиторов предъявляются в течение двух месяцев с даты опубликования "
    "настоящего сообщения. "
    "Судебное заседание по рассмотрению дела назначено на {дата следующего заседания} "
    "в помещении {арбитражного суда} по адресу: {адрес суда}."
)

_TYPES = [
    {
        "code": "realization",
        "name": "О признании гражданина банкротом и введении реализации имущества",
        "applicable_kinds": [KIND_REALIZATION],
        "blank_checkbox": KommersantMessageType.CHECKBOX_REALIZATION,
        "text_template": _TEXT_REALIZATION,
        "order": 10,
    },
    {
        "code": "restructuring",
        "name": "О признании обоснованным заявления и введении реструктуризации долгов",
        "applicable_kinds": [KIND_RESTRUCTURING],
        "blank_checkbox": KommersantMessageType.CHECKBOX_RESTRUCTURING,
        "text_template": _TEXT_RESTRUCTURING,
        "order": 20,
    },
]

_ACTION_TYPES = [
    ("kommersant_text_generated", "Сформирован текст сообщения для «Коммерсанта»", False),
    ("kommersant_blank_created", "Сформирована заявка в «Коммерсантъ»", False),
    ("kommersant_request_sent", "Заявка в «Коммерсантъ» отправлена", False),
    ("kommersant_invoice_received", "Получен счёт от «Коммерсанта»", True),
    ("kommersant_published", "Сообщение опубликовано в «Коммерсанте»", False),
]


class Command(BaseCommand):
    help = "Сид типов сообщений «Коммерсантъ» и типов лога (идемпотентно)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force-templates", action="store_true",
            help="Перезалить шаблоны текста из кода, затерев правки АУ.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        force = opts["force_templates"]

        created = updated = 0
        for spec in _TYPES:
            defaults = {
                "name": spec["name"],
                "applicable_kinds": spec["applicable_kinds"],
                "blank_checkbox": spec["blank_checkbox"],
                "order": spec["order"],
                "is_active": True,
            }
            obj, is_new = KommersantMessageType.objects.get_or_create(
                code=spec["code"],
                defaults={**defaults, "text_template": spec["text_template"], "is_draft": True},
            )
            if is_new:
                created += 1
                continue
            for field, value in defaults.items():
                setattr(obj, field, value)
            # Шаблон текста — правится АУ, перетираем только по явному флагу
            # (или если он пуст: тип завели руками и формулировку не вписали).
            if force or not (obj.text_template or "").strip():
                obj.text_template = spec["text_template"]
            obj.save()
            updated += 1

        for code, name, notifies in _ACTION_TYPES:
            ActionType.objects.update_or_create(
                code=code,
                defaults={"name": name, "is_system": True, "is_manual": False,
                          "is_active": True, "notifies": notifies},
            )

        self.stdout.write(self.style.SUCCESS(
            f"• Типы сообщений «Коммерсантъ»: создано {created}, обновлено {updated}."
        ))
        self.stdout.write(f"• Типы лога: {len(_ACTION_TYPES)} шт.")
        if not force:
            self.stdout.write(
                "  Шаблоны текста существующих типов сохранены "
                "(перезалить из кода — --force-templates)."
            )
