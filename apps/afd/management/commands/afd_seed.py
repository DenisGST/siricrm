"""Идемпотентный сидинг АФД: дефолтный исполнитель, шаблон договора БФЛ, пункт меню.

Запуск:  python manage.py afd_seed
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.afd.models import DocumentTemplate, ExecutorOrg
from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3

_DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_SEED_DOCX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "seed_templates", "contract_bfl.docx",
)


class Command(BaseCommand):
    help = "Сидинг АФД (исполнитель по умолчанию, шаблон договора БФЛ, пункт меню)"

    def handle(self, *args, **opts):
        self._seed_executor()
        self._seed_template()
        self._seed_menu()
        self.stdout.write(self.style.SUCCESS("АФД: сидинг завершён."))

    def _seed_executor(self):
        if ExecutorOrg.objects.exists():
            self.stdout.write("• Исполнитель уже есть — пропуск.")
            return
        ExecutorOrg.objects.create(
            name="Основной исполнитель",
            intro_text="",
            requisites="",
            signer_name="",
            is_default=True,
            is_active=True,
        )
        self.stdout.write(self.style.WARNING(
            "• Создан пустой исполнитель «Основной исполнитель» — "
            "заполните реквизиты в разделе АФД."
        ))

    def _seed_template(self):
        if DocumentTemplate.objects.filter(
            kind=DocumentTemplate.KIND_CONTRACT_BFL, is_active=True
        ).exists():
            self.stdout.write("• Шаблон договора БФЛ уже есть — пропуск.")
            return
        if not os.path.exists(_SEED_DOCX):
            self.stdout.write(self.style.ERROR(
                f"• Файл шаблона не найден: {_SEED_DOCX} — пропуск. "
                "Загрузите шаблон вручную в разделе АФД."
            ))
            return
        with open(_SEED_DOCX, "rb") as f:
            data = f.read()
        bucket, key = upload_file_to_s3(
            data, prefix="afd/templates",
            filename="contract_bfl.docx", content_type=_DOCX_CT,
        )
        sf = StoredFile.objects.create(
            bucket=bucket, key=key, filename="Шаблон договора БФЛ.docx",
            content_type=_DOCX_CT, size=len(data),
        )
        DocumentTemplate.objects.create(
            name="Договор юруслуг по банкротству (БФЛ)",
            kind=DocumentTemplate.KIND_CONTRACT_BFL,
            stored_file=sf,
            description="Плейсхолдеры: {Фамилия} {Имя} {Отчество} {дата рождения} "
                        "{паспорт_серия} {паспорт_номер} {паспорт_выдан_где} "
                        "{паспорт_выдан_когда} {адрес_регистрации} {индекс} "
                        "{номер_телефона} {numb_dogovor} {date_dogovor} {регион} "
                        "{сумма_юруслуги} {1платеж}..{12платеж} {сумма_сбор} "
                        "{сумма_почта} {сумма_финуправляющий} {сумма_публикации} "
                        "{summDop} {ispolnitel} {Реквизиты_исполнителя} "
                        "{Исполнитель} {Заказчик}",
            is_active=True,
        )
        self.stdout.write(self.style.SUCCESS("• Загружен шаблон договора БФЛ."))

    def _seed_menu(self):
        from apps.core.models import DashboardConfig, MenuItem
        item, created = MenuItem.objects.get_or_create(
            url="/afd/",
            defaults={
                "name": "АФД — документы",
                "icon": "file-text",
                "section": "Инструменты",
                "order": 50,
                "use_htmx": True,
                "requires_elevated": True,
                "is_active": True,
            },
        )
        # Привязываем пункт меню ко всем активным дашборд-конфигам.
        for cfg in DashboardConfig.objects.filter(is_active=True):
            cfg.menu_items.add(item)
        self.stdout.write(
            "• Пункт меню АФД " + ("создан." if created else "уже есть.")
        )
