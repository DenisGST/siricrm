"""dumpdata_tables: Django dumpdata выбранных моделей → gzip → S3 → presigned URL.

Используется как «источниковый» этап в push_tables (вызывается локально на dev)
и в pull_tables (вызывается через агента на проде).

В отличие от backup (pg_dump всей БД) — здесь только данные выбранных моделей
в JSON-формате Django-фикстур. На целевой стороне применяется через loaddata,
которое работает как UPSERT по primary key.
"""
import gzip
import io
import os
import time
from pathlib import Path

from django.apps import apps
from django.core.management import call_command

# Переиспользуем S3-клиент и бакет-резолвер из backup.py — единая логика выбора
# ключей и endpoint'а для backup-бакета (исторически прод использует
# AWS_BACKUP_BUCKET_NAME, а не AWS_BACKUP_BUCKET — поэтому дублировать опасно).
from apps.devops.handlers.backup import _s3_client as _s3_backup_client
from apps.devops.tasks import register_handler


BACKUP_DIR = Path("/app/backups")


def _backup_bucket() -> str:
    """Тот же путь резолвинга что в backup.py."""
    return os.environ.get("AWS_BACKUP_BUCKET_NAME") or os.environ["AWS_STORAGE_BUCKET_NAME"]


def _validate_models(model_labels: list[str]) -> list[str]:
    """Проверяет что все 'app.Model' существуют. Возвращает список найденных."""
    valid: list[str] = []
    for label in model_labels:
        try:
            model = apps.get_model(label)
        except (LookupError, ValueError):
            raise ValueError(f"Модель не найдена: {label}")
        if model._meta.abstract or model._meta.proxy:
            raise ValueError(f"Модель {label} — абстрактная/прокси, выгружать нельзя")
        valid.append(f"{model._meta.app_label}.{model._meta.model_name}")
    return valid


@register_handler("dumpdata_tables")
def run_dumpdata_tables(params: dict) -> dict:
    """Выгружает данные выбранных моделей в JSON.gz, заливает в S3, возвращает URL.

    params:
      - models: list[str] вида ["app.Model", ...]

    result:
      - download_url: pre-signed URL для скачивания (10 минут)
      - s3_bucket, s3_key, size_bytes, size_mb, models, object_count, finished_at
    """
    model_labels = params.get("models") or []
    if not model_labels:
        raise ValueError("Не выбрано ни одной модели для выгрузки")

    valid_labels = _validate_models(model_labels)
    log = [f"Выгрузка моделей: {', '.join(valid_labels)}"]

    # 1. dumpdata в память (потом сжимаем). natural-foreign/primary не используем —
    # для типичных reference-таблиц достаточно простой PK-based сериализации.
    buf = io.StringIO()
    call_command(
        "dumpdata", *valid_labels,
        stdout=buf, indent=2, use_natural_primary_keys=False,
        use_natural_foreign_keys=False,
    )
    json_data = buf.getvalue().encode("utf-8")
    log.append(f"  dumpdata: {len(json_data):,} байт JSON")

    # Грубый подсчёт объектов: количество "model" вхождений в JSON
    object_count = json_data.count(b'"model"')
    log.append(f"  объектов: {object_count}")

    # 2. Сжимаем
    gz_bytes = gzip.compress(json_data, compresslevel=6)
    log.append(f"  gzip: {len(gz_bytes):,} байт")

    # 3. Сохраняем локально и заливаем в S3
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"tables-{ts}.json.gz"
    local_path = BACKUP_DIR / filename
    local_path.write_bytes(gz_bytes)
    log.append(f"  локально: {local_path}")

    bucket = _backup_bucket()
    s3_key = f"tables-dumps/{filename}"
    s3 = _s3_backup_client()
    # Загрузка через pre-signed PUT — обходит баг boto3+Beget на content-sha256.
    # ВАЖНО: ContentType должен быть и в presigned URL params, и в request header,
    # иначе подпись не сойдётся → 403. Тот же паттерн что в handlers/backup.py.
    put_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": s3_key, "ContentType": "application/gzip"},
        ExpiresIn=600,
        HttpMethod="PUT",
    )
    import requests
    resp = requests.put(
        put_url, data=gz_bytes,
        headers={"Content-Type": "application/gzip"},
        timeout=300,
    )
    if not resp.ok:
        raise RuntimeError(f"S3 PUT failed: HTTP {resp.status_code} — {resp.text[:300]}")
    log.append(f"  S3 PUT: {bucket}/{s3_key} ({resp.status_code})")

    # 4. Pre-signed download URL для последующего GET (10 минут — достаточно для пайплайна)
    download_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": s3_key},
        ExpiresIn=600,
    )

    size_mb = round(len(gz_bytes) / 2**20, 2)
    log.append(f"  размер: {size_mb} MB")

    return {
        "output": "\n".join(log),
        "result": {
            "models": valid_labels,
            "object_count": object_count,
            "s3_bucket": bucket,
            "s3_key": s3_key,
            "download_url": download_url,
            "local_path": str(local_path),
            "size_bytes": len(gz_bytes),
            "size_mb": size_mb,
        },
    }
