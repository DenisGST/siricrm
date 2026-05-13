"""loaddata_tables: скачать JSON-фикстуру и применить через manage.py loaddata.

Django loaddata по умолчанию делает UPSERT по primary key: если объект с таким
PK уже есть — обновляется, иначе вставляется. Строки на цели которых НЕТ
в дампе — не трогаются.

Перед загрузкой ОБЯЗАТЕЛЬНО делаем полный pg_dump-бэкап текущей БД цели
(страховка на случай, если фикстура испорчена или не соответствует схеме).
"""
import gzip
import time
from pathlib import Path

import requests
from django.apps import apps
from django.core.management import call_command
from django.utils import timezone

from apps.devops.handlers.backup import run_backup
from apps.devops.handlers.pull_db import _download_from_s3
from apps.devops.tasks import register_handler


BACKUP_DIR = Path("/app/backups")


@register_handler("loaddata_tables")
def run_loaddata_tables(params: dict) -> dict:
    """params:
      - download_url: pre-signed URL для скачивания фикстуры (или s3_bucket+s3_key)
      - models: list[str] (для логирования; реально применяется всё что в фикстуре)
      - source_label: текстовая пометка откуда (для лога)
      - safety_backup: bool (default True) — делать ли полный бэкап БД перед load
    """
    download_url = params.get("download_url")
    s3_bucket = params.get("s3_bucket")
    s3_key = params.get("s3_key")
    models_hint = params.get("models") or []
    source_label = params.get("source_label") or "источник"
    safety_backup = params.get("safety_backup", True)

    if not download_url and not (s3_bucket and s3_key):
        raise ValueError("нужен download_url или (s3_bucket, s3_key)")

    log: list[str] = [f"Источник: {source_label}"]
    if models_hint:
        log.append(f"Ожидаемые модели: {', '.join(models_hint)}")
    result: dict = {"source": source_label, "models": models_hint}

    # 1. Защитный полный бэкап текущей БД (отдельный от tables-дампа).
    if safety_backup:
        log.append("\n=== Защитный полный бэкап БД ===")
        try:
            b = run_backup({})
            log.append(b.get("output", ""))
            result["safety_backup"] = b.get("result", {})
        except Exception as e:
            raise RuntimeError(f"Защитный бэкап не удался, loaddata отменён: {e}")
    else:
        log.append("⚠ Защитный бэкап пропущен (safety_backup=False).")

    # 2. Скачиваем фикстуру
    log.append("\n=== Скачивание фикстуры ===")
    if download_url:
        resp = requests.get(download_url, timeout=300)
        resp.raise_for_status()
        gz_bytes = resp.content
    else:
        gz_bytes = _download_from_s3(s3_bucket, s3_key)
    log.append(f"  Скачано {len(gz_bytes):,} байт (gzip)")

    json_size = len(gzip.decompress(gz_bytes))
    log.append(f"  Распакованный размер: {json_size:,} байт JSON")

    # Сохраняем .json.gz как «снимок источника». Django loaddata умеет читать .gz
    # напрямую, поэтому несжатый .json не пишем (он бы дал конфликт «multiple
    # fixtures with same label» в find_fixtures).
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = (s3_key or "incoming-tables").rsplit("/", 1)[-1]
    if not name.endswith(".json.gz"):
        name = f"incoming-tables-{time.strftime('%Y%m%d-%H%M%S')}.json.gz"
    local_gz = BACKUP_DIR / name
    local_gz.write_bytes(gz_bytes)
    log.append(f"  Сохранено локально: {local_gz}")

    # 3. Считаем сколько объектов до загрузки (для отчёта дельты)
    before_counts: dict[str, int] = {}
    for label in models_hint:
        try:
            model = apps.get_model(label)
            before_counts[label] = model.objects.count()
        except Exception:
            pass

    # 4. loaddata прямо из .json.gz (Django умеет читать сжатые фикстуры).
    # По умолчанию UPSERT по pk: совпадение PK → UPDATE, новые → INSERT.
    log.append("\n=== loaddata (UPSERT по pk) ===")
    import io
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        call_command("loaddata", str(local_gz), stdout=out_buf, stderr=err_buf)
    except Exception as e:
        # Не падаем без логов — собираем что есть
        log.append(f"  ⚠ loaddata выбросил: {e}")
        if out_buf.getvalue():
            log.append(f"  stdout: {out_buf.getvalue()}")
        if err_buf.getvalue():
            log.append(f"  stderr: {err_buf.getvalue()}")
        raise
    log.append(f"  {out_buf.getvalue().strip()}")
    if err_buf.getvalue():
        log.append(f"  stderr: {err_buf.getvalue().strip()}")

    # 5. Счёт после
    after_counts: dict[str, int] = {}
    delta: dict[str, int] = {}
    for label in models_hint:
        try:
            model = apps.get_model(label)
            after_counts[label] = model.objects.count()
            delta[label] = after_counts[label] - before_counts.get(label, 0)
        except Exception:
            pass
    log.append("\n=== Изменения по моделям ===")
    for label in models_hint:
        if label in after_counts:
            log.append(
                f"  {label}: {before_counts.get(label, 0)} → {after_counts[label]} "
                f"(дельта {delta.get(label, 0):+d})"
            )

    result.update({
        "before_counts": before_counts,
        "after_counts": after_counts,
        "delta": delta,
        "local_path": str(local_gz),
        "size_bytes": len(gz_bytes),
        "size_mb": round(len(gz_bytes) / 2**20, 2),
        "finished_at": timezone.now().isoformat(),
    })
    return {"output": "\n".join(log), "result": result}
