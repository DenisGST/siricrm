"""Идемпотентный сид раздела «Процедуры банкротства».

Заводит:
  1. Каталог стадий (ProcedureStage) — общие + по видам процедур.
  2. DRAFT-каталог обязательных мероприятий (MilestoneTemplate) — заглушка,
     🛑 состав и сроки подлежат подтверждению АУ в админке.
  3. Типы событий для лога/уведомлений (EventType).

Повторный запуск безопасен (update_or_create по code).
"""
from django.core.management.base import BaseCommand

from apps.crm.models import EventType
from apps.procedure.models import (
    KIND_REALIZATION,
    KIND_RESTRUCTURING,
    SCOPE_COMMON,
    MilestoneTemplate,
    ProcedureStage,
    RequestPackage,
    RequestType,
)

# (code, name, kind_scope, order, is_terminal)
STAGES = [
    ("prep", "Подготовка (сбор документов)", SCOPE_COMMON, 10, False),
    ("filing", "Подача заявления в суд", SCOPE_COMMON, 20, False),
    ("accept", "Принятие судом / первое заседание", SCOPE_COMMON, 30, False),
    ("restr_start", "Реструктуризация: начало", KIND_RESTRUCTURING, 40, False),
    ("restr_run", "Реструктуризация: ход процедуры", KIND_RESTRUCTURING, 50, False),
    ("restr_done", "Реструктуризация: завершение", KIND_RESTRUCTURING, 60, False),
    ("real_start", "Реализация: начало", KIND_REALIZATION, 70, False),
    ("real_auction", "Реализация: торги", KIND_REALIZATION, 80, False),
    ("real_done", "Реализация: завершение", KIND_REALIZATION, 90, False),
    ("closed", "Завершено", SCOPE_COMMON, 100, True),
]

# DRAFT-каталог мероприятий: (code, stage_code, title, base_date_key, offset_days, order)
# 🛑 Сроки — ЗАГЛУШКА для демонстрации движка. Подтвердить с АУ.
MILESTONES = [
    # Общая фаза
    ("filing_submit", "filing", "Подать заявление в суд", "", 0, 10),
    # Реструктуризация
    ("restr_pub_efrsb", "restr_start", "Публикация в ЕФРСБ о введении реструктуризации",
     "proc_intro_date", 3, 10),
    ("restr_pub_kommersant", "restr_start", "Публикация в «Коммерсантъ»",
     "proc_intro_date", 10, 20),
    ("restr_notify", "restr_start", "Направить уведомления кредиторам",
     "proc_publication_efrsb_date", 14, 30),
    ("restr_register_close", "restr_run", "Закрытие реестра требований кредиторов",
     "proc_publication_efrsb_date", 60, 10),
    ("restr_meeting", "restr_run", "Первое собрание кредиторов",
     "proc_publication_efrsb_date", 75, 20),
    ("restr_report", "restr_done", "Отчёт финуправляющего в суд", "", 0, 10),
    # Реализация
    ("real_pub_efrsb", "real_start", "Публикация в ЕФРСБ о введении реализации",
     "proc_intro_date", 3, 10),
    ("real_pub_kommersant", "real_start", "Публикация в «Коммерсантъ»",
     "proc_intro_date", 10, 20),
    ("real_notify", "real_start", "Направить уведомления кредиторам",
     "proc_publication_efrsb_date", 14, 30),
    ("real_register_close", "real_start", "Закрытие реестра требований кредиторов",
     "proc_publication_efrsb_date", 60, 40),
    ("real_inventory", "real_start", "Опись имущества должника", "", 0, 50),
    ("real_auction_pub", "real_auction", "Публикация о торгах", "", 0, 10),
    ("real_report", "real_done", "Завершающий отчёт финуправляющего в суд", "", 0, 10),
]

# (code, name, source, notifies, is_manual, notify_hint)
EVENT_TYPES = [
    ("procedure_milestone_overdue", "Просрочено мероприятие процедуры", "system",
     True, False, "Проверьте срок и закройте мероприятие"),
    ("procedure_stage_changed", "Смена стадии процедуры", "employee", False, False, ""),
    ("procedure_added", "Добавлена процедура", "court", False, False, ""),
    # Источник даты «Передача документов на подготовку иска» (п.4 дат услуги).
    ("claim_prep_assigned", "Передано на подготовку иска", "employee", False, True, ""),
    ("request_overdue", "Просрочен ответ на запрос", "system",
     True, False, "Проверьте, пришёл ли ответ; при необходимости — повторный запрос"),
]

# DRAFT-каталог типов запросов: (code, name, response_days, order)
# 🛑 ЗАГЛУШКА. Госорган по умолчанию и сроки уточняет юрист в Справочниках.
REQUEST_TYPES = [
    ("req_rosreestr", "Запрос в Росреестр (недвижимость)", 30, 10),
    ("req_gibdd", "Запрос в ГИБДД/МРЭО (транспорт)", 30, 20),
    ("req_gostehnadzor", "Запрос в Гостехнадзор (самоходная техника)", 30, 30),
    ("req_gims", "Запрос в ГИМС (маломерные суда)", 30, 40),
    ("req_fns", "Запрос в ФНС (счета, доли, ИП, доходы)", 30, 50),
    ("req_sfr", "Запрос в СФР/ПФР (выплаты, СНИЛС, работодатели)", 30, 60),
    ("req_zags", "Запрос в ЗАГС (акты гражданского состояния)", 30, 70),
    ("req_bank", "Запрос в банк (счета, остатки, движение)", 30, 80),
    ("req_employment", "Запрос в центр занятости", 30, 90),
]

# DRAFT-пакеты: (code, name, [type_codes], order)
REQUEST_PACKAGES = [
    ("pkg_basic", "Базовый пакет запросов",
     ["req_rosreestr", "req_gibdd", "req_fns", "req_sfr", "req_zags", "req_bank"], 10),
    ("pkg_full", "Расширенный пакет запросов",
     ["req_rosreestr", "req_gibdd", "req_gostehnadzor", "req_gims", "req_fns",
      "req_sfr", "req_zags", "req_bank", "req_employment"], 20),
]


class Command(BaseCommand):
    help = "Сид стадий, DRAFT-мероприятий и типов событий раздела процедур"

    def handle(self, *args, **opts):
        stages = {}
        for code, name, scope, order, terminal in STAGES:
            stage, _ = ProcedureStage.objects.update_or_create(
                code=code,
                defaults={"name": name, "kind_scope": scope, "order": order,
                          "is_terminal": terminal, "is_active": True},
            )
            stages[code] = stage
        self.stdout.write(self.style.SUCCESS(f"Стадии: {len(stages)}"))

        for code, stage_code, title, base_key, offset, order in MILESTONES:
            MilestoneTemplate.objects.update_or_create(
                code=code,
                defaults={"stage": stages[stage_code], "title": title,
                          "base_date_key": base_key, "offset_days": offset,
                          "is_mandatory": True, "order": order,
                          "is_active": True, "is_draft": True},
            )
        self.stdout.write(self.style.SUCCESS(f"Шаблоны мероприятий: {len(MILESTONES)}"))

        for code, name, source, notifies, is_manual, hint in EVENT_TYPES:
            EventType.objects.update_or_create(
                code=code,
                defaults={"name": name, "source": source, "notifies": notifies,
                          "is_manual": is_manual, "is_system": True,
                          "notify_hint": hint, "is_active": True},
            )
        self.stdout.write(self.style.SUCCESS(f"Типы событий: {len(EVENT_TYPES)}"))

        rtypes = {}
        for code, name, days, order in REQUEST_TYPES:
            rt, _ = RequestType.objects.update_or_create(
                code=code,
                defaults={"name": name, "response_days": days, "order": order,
                          "is_active": True, "is_draft": True},
            )
            rtypes[code] = rt
        self.stdout.write(self.style.SUCCESS(f"Типы запросов: {len(rtypes)}"))

        for code, name, type_codes, order in REQUEST_PACKAGES:
            pkg, _ = RequestPackage.objects.update_or_create(
                code=code,
                defaults={"name": name, "order": order, "is_active": True, "is_draft": True},
            )
            pkg.types.set([rtypes[tc] for tc in type_codes if tc in rtypes])
        self.stdout.write(self.style.SUCCESS(f"Пакеты запросов: {len(REQUEST_PACKAGES)}"))

        self.stdout.write(self.style.WARNING(
            "🛑 DRAFT: состав и сроки мероприятий — заглушка. "
            "Подтвердите/отредактируйте в админке (Шаблоны мероприятий) с АУ."
        ))
