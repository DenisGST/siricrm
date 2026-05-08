"""
Генерация шаблона анкеты для услуги БФЛ (Банкротство физ. лиц).
Запустить: python manage.py create_bfl_questionnaire [--reset]
"""
from django.core.management.base import BaseCommand
from apps.crm.models import ServiceName, LegalEntityKind
from apps.questionnaire.models import (
    QuestionnaireTemplate, QuestionnairePage, Question, QuestionChoice,
)


def _q(page, order, text, qtype, hint="", required=False, allow_custom=False, le_kind=None, default=""):
    return Question.objects.create(
        page=page, order=order, text=text,
        question_type=qtype, hint=hint,
        is_required=required, allow_custom_text=allow_custom,
        legal_entity_kind=le_kind, default_value=default,
    )


def _choices(q, items):
    """items: list of str or (text, has_extra, extra_hint) or (text, has_extra, extra_hint, extra_type)"""
    for i, item in enumerate(items):
        if isinstance(item, str):
            text, has_extra, extra_hint, extra_type = item, False, "", "text"
        elif len(item) == 2:
            text, has_extra = item; extra_hint, extra_type = "", "text"
        elif len(item) == 3:
            text, has_extra, extra_hint = item; extra_type = "text"
        else:
            text, has_extra, extra_hint, extra_type = item
        QuestionChoice.objects.create(
            question=q, text=text, order=i,
            has_extra_field=bool(has_extra),
            extra_field_hint=extra_hint or "",
            extra_field_type=extra_type or "text",
        )


class Command(BaseCommand):
    help = "Создаёт шаблон анкеты БФЛ"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Удалить существующий шаблон и создать заново")

    def handle(self, *args, **options):
        bfl = ServiceName.objects.filter(short_name="БФЛ").first()
        if not bfl:
            self.stderr.write("ServiceName 'БФЛ' не найдена")
            return

        if options["reset"]:
            QuestionnaireTemplate.objects.filter(service_name=bfl).delete()
            self.stdout.write("Существующий шаблон удалён")

        if QuestionnaireTemplate.objects.filter(service_name=bfl).exists():
            self.stderr.write("Шаблон уже существует. Используйте --reset для пересоздания.")
            return

        tmpl = QuestionnaireTemplate.objects.create(
            service_name=bfl,
            title="Анкета по банкротству физического лица",
            description="Заполняется сотрудником при первичном разговоре с клиентом",
            is_active=True,
        )

        # ═══════════════════════════════════════════════════════
        # СТРАНИЦА 1 — Общие сведения
        # ═══════════════════════════════════════════════════════
        p1 = QuestionnairePage.objects.create(template=tmpl, title="Общие сведения", order=0)

        # 1. Откуда узнали
        q1 = _q(p1, 0, "Откуда узнали про нас?", "choice", required=True)
        _choices(q1, [
            ("Интернет-реклама",                  True,  "Комментарий",         "text"),
            ("От нашего клиента",                 True,  "Выберите клиента",    "client_ref"),
            ("От нашего сотрудника",              True,  "Выберите сотрудника", "employee_ref"),
            ("От нашего агента",                  True,  "Выберите агента",     "agent_ref"),
            ("Не помню / не знаю / не скажу",     True,  "Комментарий",        "text"),
        ])

        # 2. Ранее банкротство
        q2 = _q(p1, 1, "Проходили ли ранее процедуру банкротства?", "choice", required=True)
        _choices(q2, [
            "Нет",
            "Да, более 5 лет назад",
            "Да, менее 5 лет назад",
        ])

        # 3. Регион
        _q(p1, 2, "В каком регионе проживаете?", "region_ref", required=True)

        # 4. Семейное положение
        _q(p1, 3, "Семейное положение", "marital_status", required=True)

        # ═══════════════════════════════════════════════════════
        # СТРАНИЦА 2 — Задолженности
        # ═══════════════════════════════════════════════════════
        p2 = QuestionnairePage.objects.create(template=tmpl, title="Задолженности", order=1)

        bank_kind = LegalEntityKind.objects.filter(name__icontains="банк").first()
        mfo_kind  = LegalEntityKind.objects.filter(name__icontains="микро").first()

        # 5а. Банки
        _q(p2, 0, "а) Банки — долги перед банками", "bank_debts")

        # 5б. МФО
        _q(p2, 1, "б) Микрофинансовые организации (МФО)", "mfo_debts",
           hint="⚠️ Предупредить клиента: если всплывут сведения о нераскрытых МФО — "
                "сумма по договору может быть увеличена")

        # 5в. Налоги
        _q(p2, 3, "в) Неуплаченные налоги", "tax_debts")

        # 5г. Коммунальные
        _q(p2, 4, "г) Коммунальные платежи", "utility_debts")

        # 5д. Штрафы
        _q(p2, 5, "д) Штрафы", "fine_debts")

        # 5е. По решению суда
        _q(p2, 6, "е) Задолженности по решению суда", "court_debts")

        # 5ж. Иное
        _q(p2, 7, "ж) Иные задолженности", "other_debts")

        # ═══════════════════════════════════════════════════════
        # СТРАНИЦА 3 — Имущество
        # ═══════════════════════════════════════════════════════
        p3 = QuestionnairePage.objects.create(template=tmpl, title="Имущество", order=2)

        # 6. Имущество
        _q(p3, 0, "Имущество в собственности", "property_assets")

        _q(p3, 1, "Имущество, проданное за последние 3 года", "sold_assets")

        # ═══════════════════════════════════════════════════════
        # СТРАНИЦА 4 — Дополнительная информация
        # ═══════════════════════════════════════════════════════
        p4 = QuestionnairePage.objects.create(template=tmpl, title="Дополнительная информация", order=3)

        # 8. Причины неплатежеспособности
        q8 = _q(p4, 0, "Укажите причины неплатёжеспособности", "multi_choice", required=True)
        _choices(q8, [
            ("Затрудняюсь ответить", False, ""),
            ("Увольнение с работы или снижение зарплаты", True, "Комментарий"),
            ("Болезнь", True, "Комментарий"),
            ("Попал/а под влияние мошенников", True, "Комментарий"),
            ("Развод", True, "Комментарий"),
            ("Иная причина", True, "Опишите"),
        ])

        # 9. Директор ООО
        q9 = _q(p4, 1, "Являетесь ли директором ООО, КФХ или иного юридического лица?", "choice", required=True, default="Нет")
        _choices(q9, [
            "Нет",
            "Да",
        ])
        _q(p4, 2, "Укажите наименование организации (если являетесь директором)", "legal_entity_ref",
           hint="Выберите из справочника или введите вручную",
           allow_custom=True)

        # 10. Место работы
        q10 = _q(p4, 3, "Место работы", "choice", required=True)
        _choices(q10, [
            "Официально не работаю",
            ("Работаю официально", True, "Укажите организацию и ежемесячный доход"),
            ("Работаю неофициально", True, "Описание деятельности и примерный доход"),
        ])
        _q(p4, 4, "Ежемесячный доход (руб.)", "money")

        # 11. Дети
        _q(p4, 5, "Есть ли на иждивении несовершеннолетние дети?", "children_list")

        # 12. Инвалидность
        q12 = _q(p4, 7, "Имеется ли инвалидность?", "choice", required=True)
        _choices(q12, [
            "Нет",
            ("Да", True, "Укажите группу, размер выплат и комментарий"),
        ])

        # 13. Судимость
        q13 = _q(p4, 8, "Имеется ли судимость?", "choice", required=True)
        _choices(q13, [
            "Нет",
            ("Да", True, "Статья, дата, за что"),
        ])

        # 14. Госуслуги
        q14 = _q(p4, 9, "Имеется ли подтверждённый личный кабинет на Госуслугах?", "choice", required=True)
        _choices(q14, ["Да", "Нет", "Не помню"])

        # 15. Как связаться
        q15 = _q(p4, 10, "Как и когда лучше с вами связаться?", "multi_choice")
        _choices(q15, [
            ("Телефон", True, "Удобное время для звонков"),
            ("Telegram", True, "Ник или номер"),
            ("MAX", False, ""),
            ("WhatsApp", True, "Номер"),
        ])

        # 16. Контакт родственника
        _q(p4, 11, "Контакт родственника на случай если не сможем дозвониться", "textarea",
           hint="ФИО, телефон, степень родства")

        # 17. Оплата
        q17 = _q(p4, 12, "Как предпочитаете производить оплату?", "choice", required=True)
        _choices(q17, [
            "Сразу 100%",
            "В рассрочку",
        ])

        self.stdout.write(self.style.SUCCESS(
            f"✅ Шаблон создан: {tmpl.title}\n"
            f"   Страниц: {tmpl.pages.count()}\n"
            f"   Вопросов: {Question.objects.filter(page__template=tmpl).count()}"
        ))
