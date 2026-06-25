"""Идемпотентный сидинг ЕФРСБ.

  • базовая .docx-заготовка сообщения (kind=efrsb) → S3 + DocumentTemplate;
  • DRAFT-каталог типов сообщений (EfrsbMessageType) с привязкой к заготовке;
  • типы лога (EventType/ActionType) для событийки.

🛑 НЕ входит в deploy-handler — гонять вручную (как procedure_seed):
    docker exec siricrm-web-1 python manage.py efrsb_seed

🛑 Соответствие наших кодов ↔ типам API ЕФРСБ и сроки (deadline_offset_days) —
   ЗАГОТОВКА (is_draft=True). Подтверждается с АУ при наполнении каталога.
   Все .docx-шаблоны и тексты — черновики-заготовки, заменяются в разделе АФД.
"""
from __future__ import annotations

import io

from django.core.management.base import BaseCommand

from apps.afd.models import DocumentTemplate
from apps.efrsb.models import EfrsbMessageType
from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3

_DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_TEMPLATE_NAME = "Сообщение ЕФРСБ — базовая заготовка (черновик)"

# Нейтральная структурная «рыба» сообщения. Содержательную часть («Текст сообщения»)
# вводит АУ при формировании; реквизиты должника/АУ/дела подставляются из CRM.
# Это ЗАГОТОВКА — АУ заводит полноценные пер-типовые шаблоны в разделе АФД.
_SKELETON_PARAS = [
    ("{Заголовок}", "center", True),
    ("", "both", False),
    ("Финансовый управляющий {ФИО Финансовый управляющий} (ИНН {ИНН АУ}, СНИЛС {СНИЛС АУ}, "
     "адрес для корреспонденции: {Адрес арбитражного управляющего}), член {Реквизиты СРО}, "
     "действующий в деле о банкротстве № {номер дела} ({арбитражный суд}) в отношении "
     "{Фамилия} {Имя} {Отчество} (дата рождения {дата рождения}, место рождения {место рождения}, "
     "ИНН {ИНН}, СНИЛС {СНИЛС}, адрес регистрации: {адрес регистрации}), сообщает:", "both", False),
    ("", "both", False),
    ("{Текст сообщения}", "both", False),
    ("", "both", False),
    ("Дата: {дата}", "both", False),
    ("Финансовый управляющий ________________ {ФамилияИО АУ}", "both", False),
]

# DRAFT-каталог: (code, name, api_kind, api_type, aliases, applicable_kinds,
#                 deadline_base_key, deadline_offset_days)
# 🛑 ВСЁ — заготовка, подтверждается с АУ.
_MESSAGE_TYPES = [
    ("arbitral_decree", "Судебный акт (о введении процедуры)", "message",
     "ArbitralDecree", [], [], "proc_intro_date", 0),
    ("meeting_creditors", "Сообщение о собрании кредиторов", "message",
     "Meeting", ["Meeting2"], [], "", 0),
    ("meeting_result", "Результаты собрания кредиторов", "message",
     "MeetingResult", [], [], "", 0),
    ("inventory", "Сведения о результатах инвентаризации имущества", "message",
     "PropertyInventoryResult", [], ["realization"], "", 0),
    ("evaluation", "Отчёт об оценке имущества должника", "message",
     "AssessmentReport", ["PropertyEvaluationReport"], ["realization"], "", 0),
    ("auction", "Объявление о проведении торгов", "message",
     "Auction", ["Auction2", "ChangeAuction", "ChangeAuction2"], ["realization"], "", 0),
    ("trade_result", "Сообщение о результатах торгов", "message",
     "TradeResult", [], ["realization"], "", 0),
    ("sale_contract", "Сведения о заключении договора купли-продажи", "message",
     "SaleContractResult", ["SaleContractResult2"], ["realization"], "", 0),
    ("demand_announcement", "Извещение о возможности предъявления требований", "message",
     "DemandAnnouncement", [], [], "proc_publication_efrsb_date", 0),
    ("fin_state", "Информация о финансовом состоянии", "message",
     "FinancialStateInformation", [], [], "", 0),
    ("deliberate_bankruptcy", "Признаки преднамеренного/фиктивного банкротства", "message",
     "DeliberateBankruptcy", [], [], "", 0),
    ("calc_order", "Сведения о порядке и сроках расчётов с кредиторами", "message",
     "OrderAndTimingCalculations", [], [], "", 0),
    ("restr_plan_view", "Ознакомление с проектом плана реструктуризации", "message",
     "ViewDraftRestructuringPlan", [], ["restructuring"], "", 0),
    ("final_report", "Финальный отчёт финансового управляющего", "report",
     "Final", ["Final2"], [], "", 0),
    ("other", "Иное сообщение", "message",
     "Other", [], [], "", 0),
]

# 🛑 «Вводные» типы — проставляют Procedure.publication_efrsb_date. ЗАГОТОВКА, TO CONFIRM:
# судебный акт о введении процедуры публикуется в ЕФРСБ как «Сообщение о судебном акте».
_SETS_DATE_CODES = {"arbitral_decree"}

_EVENT_TYPES = [
    # code, name, source, notifies, hint
    ("efrsb_published_detected", "Публикация в ЕФРСБ обнаружена", "system", False, ""),
    ("efrsb_violation", "Публикация ЕФРСБ с нарушением срока", "system", True,
     "ЕФРСБ отметил публикацию как сделанную с нарушением срока — проверьте."),
]
_ACTION_TYPES = [
    # code, name, notifies
    ("efrsb_text_generated", "Сформирован текст сообщения ЕФРСБ", False),
]


def _build_skeleton_docx() -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    align_map = {
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "both": WD_ALIGN_PARAGRAPH.JUSTIFY,
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
    }
    doc = Document()
    for text, align, bold in _SKELETON_PARAS:
        p = doc.add_paragraph()
        p.alignment = align_map.get(align, WD_ALIGN_PARAGRAPH.JUSTIFY)
        run = p.add_run(text)
        run.bold = bold
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


class Command(BaseCommand):
    help = "Идемпотентный сидинг ЕФРСБ (заготовка-шаблон + DRAFT-каталог типов + типы лога)."

    def handle(self, *args, **opts):
        tpl = self._seed_template()
        self._seed_types(tpl)
        self._seed_log_types()
        self.stdout.write(self.style.SUCCESS("ЕФРСБ: сидинг завершён."))

    def _seed_template(self) -> DocumentTemplate | None:
        tpl = DocumentTemplate.objects.filter(
            kind=DocumentTemplate.KIND_EFRSB, name=_TEMPLATE_NAME
        ).first()
        if tpl:
            self.stdout.write("• Заготовка-шаблон ЕФРСБ уже есть — пропуск.")
            return tpl
        try:
            data = _build_skeleton_docx()
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(
                f"• Не удалось собрать заготовку .docx ({exc}). "
                "Заведите шаблон вручную в разделе АФД."
            ))
            return None
        bucket, key = upload_file_to_s3(
            data, prefix="afd/efrsb_templates",
            filename="efrsb_message_skeleton.docx", content_type=_DOCX_CT,
        )
        sf = StoredFile.objects.create(
            bucket=bucket, key=key, filename=f"{_TEMPLATE_NAME}.docx",
            content_type=_DOCX_CT, size=len(data),
        )
        tpl = DocumentTemplate.objects.create(
            name=_TEMPLATE_NAME,
            kind=DocumentTemplate.KIND_EFRSB,
            stored_file=sf,
            description=(
                "Черновик-заготовка сообщения ЕФРСБ. Плейсхолдеры: {Заголовок} "
                "{Текст сообщения} {Фамилия} {Имя} {Отчество} {дата рождения} "
                "{место рождения} {ИНН} {СНИЛС} {адрес регистрации} "
                "{ФИО Финансовый управляющий} {ФамилияИО АУ} {ИНН АУ} {СНИЛС АУ} "
                "{Адрес арбитражного управляющего} {Реквизиты СРО} {арбитражный суд} "
                "{номер дела} {дата}. Замените на пер-типовые шаблоны в разделе АФД."
            ),
        )
        self.stdout.write(self.style.WARNING(
            "• Создана базовая заготовка-шаблон ЕФРСБ (черновик) — "
            "доработайте/разнесите по типам в разделе АФД."
        ))
        return tpl

    def _seed_types(self, tpl):
        created = updated = 0
        for (code, name, api_kind, api_type, aliases, kinds, base_key, offset) in _MESSAGE_TYPES:
            obj, was_created = EfrsbMessageType.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "api_kind": api_kind,
                    "api_type": api_type,
                    "api_type_aliases": aliases,
                    "applicable_kinds": kinds,
                    "deadline_base_key": base_key,
                    "deadline_offset_days": offset,
                    "sets_efrsb_date": code in _SETS_DATE_CODES,
                    "is_active": True,
                    "is_draft": True,
                },
            )
            # Шаблон привязываем только если ещё не задан (не перетираем ручную правку).
            if tpl and obj.template_id is None:
                obj.template = tpl
                obj.save(update_fields=["template", "updated_at"])
            created += int(was_created)
            updated += int(not was_created)
        self.stdout.write(self.style.SUCCESS(
            f"• Типы сообщений ЕФРСБ: создано {created}, обновлено {updated} (все DRAFT)."
        ))

    def _seed_log_types(self):
        from apps.crm.models import ActionType, EventType
        for code, name, source, notifies, hint in _EVENT_TYPES:
            EventType.objects.update_or_create(
                code=code,
                defaults={"name": name, "source": source, "is_system": True,
                          "is_manual": False, "is_active": True,
                          "notifies": notifies, "notify_hint": hint},
            )
        for code, name, notifies in _ACTION_TYPES:
            ActionType.objects.update_or_create(
                code=code,
                defaults={"name": name, "is_system": True, "is_manual": False,
                          "is_active": True, "notifies": notifies},
            )
        self.stdout.write("• Типы лога (EventType/ActionType) ЕФРСБ обновлены.")
