"""Загрузка .docx-шаблонов запросов из OLD/Шаблоны запросов → S3 (AFD
DocumentTemplate kind=request) + привязка к типам запросов (RequestType).

Идемпотентно: если у типа уже есть шаблон — пропускаем (повторно грузим только
с --force). Запускать вручную при выкатке/обновлении шаблонов.
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.afd.models import DocumentTemplate
from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3
from apps.procedure.models import RequestType

DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUBDIR = Path("OLD") / "Шаблоны запросов"

# filename → (RequestType.code, RequestType.name)
MAP = {
    "Запрос в МРЭО.docx": ("req_gibdd", "Запрос в ГИБДД/МРЭО (транспорт)"),
    "Запрос в ГИМС.docx": ("req_gims", "Запрос в ГИМС (маломерные суда)"),
    "Запрос в Гостехнадзор.docx": ("req_gostehnadzor", "Запрос в Гостехнадзор (самоходная техника)"),
    "Запрос в ДМИ.docx": ("req_dmi", "Запрос в ДМИ (муниципальное имущество)"),
    "Запрос в ИФНС о расчетных счетах.docx": ("req_fns", "Запрос в ИФНС (расчётные счета)"),
    "Запрос в ИФНС об участии в организациях.docx": ("req_fns_orgs", "Запрос в ИФНС (участие в организациях)"),
    "Запрос в ПФР об участии в организациях.docx": ("req_sfr", "Запрос в ПФР/СФР (участие в организациях)"),
    "Запрос в суд.docx": ("req_court", "Запрос в суд"),
    "Запрос ИНОЕ.docx": ("req_other", "Запрос (иное)"),
    "Информационные письма в Банки.docx": ("req_bank", "Информационное письмо в банк"),
    "Информационные письма в Госорганы.docx": ("req_info_gov", "Информационное письмо в госорган"),
}

ORDER = {code: (i + 1) * 10 for i, code in enumerate(c for _, (c, _n) in MAP.items())}


class Command(BaseCommand):
    help = "Загрузить .docx-шаблоны запросов в S3 и привязать к типам запросов"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="перезалить даже если шаблон уже привязан")

    def handle(self, *args, **opts):
        force = opts["force"]
        base = Path(settings.BASE_DIR) / SUBDIR
        if not base.exists():
            self.stderr.write(self.style.ERROR(f"Нет папки {base}"))
            return
        loaded = skipped = missing = 0
        for fname, (code, name) in MAP.items():
            path = base / fname
            if not path.exists():
                self.stderr.write(self.style.WARNING(f"нет файла: {fname}"))
                missing += 1
                continue
            rt, _ = RequestType.objects.get_or_create(
                code=code,
                defaults={"name": name, "order": ORDER.get(code, 0),
                          "is_active": True, "is_draft": True},
            )
            if rt.template_id and not force:
                skipped += 1
                continue
            data = path.read_bytes()
            bucket, key = upload_file_to_s3(
                data, prefix="afd/request_templates", filename=fname, content_type=DOCX_CT,
            )
            sf = StoredFile.objects.create(
                bucket=bucket, key=key, filename=fname, content_type=DOCX_CT, size=len(data),
            )
            tpl = DocumentTemplate.objects.create(
                name=name, kind=DocumentTemplate.KIND_REQUEST, stored_file=sf,
                description="Шаблон запроса (плейсхолдеры {…}).", is_active=True,
            )
            rt.template = tpl
            if not rt.name:
                rt.name = name
            rt.save(update_fields=["template", "name", "updated_at"])
            loaded += 1
            self.stdout.write(self.style.SUCCESS(f"✓ {fname} → {code}"))
        self.stdout.write(self.style.SUCCESS(
            f"Готово: загружено {loaded}, пропущено {skipped}, нет файла {missing}."
        ))
