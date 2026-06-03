"""Сидинг АФД при миграции (для prod, где нет shell-доступа для afd_seed).

Идемпотентно: дефолтный (пустой) ExecutorOrg, шаблон договора БФЛ из
apps/afd/seed_templates/contract_bfl.docx → S3, пункт меню «АФД — документы».
Загрузка в S3 обёрнута в try/except — если упадёт, миграция не падает,
шаблон можно загрузить вручную в UI /afd/.
"""
import os

from django.db import migrations

_DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_SEED_DOCX = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "seed_templates", "contract_bfl.docx",
)


def seed(apps, schema_editor):
    ExecutorOrg = apps.get_model("afd", "ExecutorOrg")
    DocumentTemplate = apps.get_model("afd", "DocumentTemplate")
    StoredFile = apps.get_model("files", "StoredFile")
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")

    # 1. Исполнитель по умолчанию (пустой — реквизиты заполняются в UI).
    if not ExecutorOrg.objects.exists():
        ExecutorOrg.objects.create(
            name="Основной исполнитель", intro_text="", requisites="",
            signer_name="", is_default=True, is_active=True,
        )

    # 2. Шаблон договора БФЛ → S3 (best-effort).
    if not DocumentTemplate.objects.filter(kind="contract_bfl", is_active=True).exists():
        try:
            from apps.files.s3_utils import upload_file_to_s3
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
                kind="contract_bfl", stored_file=sf,
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
        except Exception:
            # S3 недоступен/файл отсутствует — шаблон загрузят вручную в /afd/.
            pass

    # 3. Пункт меню «АФД — документы».
    item, _ = MenuItem.objects.get_or_create(
        url="/afd/",
        defaults={
            "name": "АФД — документы", "icon": "file-text",
            "section": "Инструменты", "order": 50, "use_htmx": True,
            "requires_elevated": True, "is_active": True,
        },
    )
    for cfg in DashboardConfig.objects.filter(is_active=True):
        cfg.menu_items.add(item)


def unseed(apps, schema_editor):
    # Откат не трогает данные (безопасно для прод-данных).
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("afd", "0001_initial"),
        ("core", "0018_department_can_edit_payment_schedule"),
        ("files", "0004_storedfile_bubble_id"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
