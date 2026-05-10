"""Backup handler: pg_dump → gzip → S3 + локальная папка."""
import gzip
import os
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.client import Config

from apps.devops.tasks import register_handler


BACKUP_DIR = Path("/app/backups")


def _docker_client():
    import docker
    return docker.from_env()


def _exec_pg_dump() -> bytes:
    """Выполняет pg_dump в контейнере БД и возвращает сырые байты SQL."""
    db_user = os.environ["POSTGRES_USER"]
    db_name = os.environ["POSTGRES_DB"]
    db_password = os.environ["POSTGRES_PASSWORD"]

    client = _docker_client()
    # имя контейнера БД в compose-проекте
    candidates = ["siricrm-db-1", "siricrm_db_1"]
    db_container = None
    for name in candidates:
        try:
            db_container = client.containers.get(name)
            break
        except Exception:
            continue
    if db_container is None:
        raise RuntimeError(f"DB container not found (tried {candidates})")

    # exec_run возвращает (exit_code, output_bytes)
    exit_code, output = db_container.exec_run(
        cmd=["pg_dump", "-U", db_user, "-d", db_name, "--no-owner", "--no-acl"],
        environment={"PGPASSWORD": db_password},
        demux=False,
    )
    if exit_code != 0:
        raise RuntimeError(f"pg_dump failed (exit={exit_code}): {output[:500]!r}")
    return output


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_S3_BASE_URL"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_S3_REGION_NAME", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            s3={"payload_signing_enabled": False, "addressing_style": "path"},
        ),
    )


@register_handler("backup")
def run_backup(params: dict) -> dict:
    """Создаёт бэкап БД и заливает в S3."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"db-{ts}.sql.gz"
    local_path = BACKUP_DIR / filename
    bucket = os.environ.get("AWS_BACKUP_BUCKET_NAME") or os.environ["AWS_STORAGE_BUCKET_NAME"]
    s3_key = f"db-backups/{filename}"

    output_lines = [f"Запуск pg_dump..."]
    sql_bytes = _exec_pg_dump()
    output_lines.append(f"  pg_dump OK: {len(sql_bytes)} байт сырого SQL")

    output_lines.append("Сжатие gzip и запись на диск...")
    with gzip.open(local_path, "wb") as f:
        f.write(sql_bytes)
    size = local_path.stat().st_size
    output_lines.append(f"  Локальный файл: {local_path} ({size:,} байт)")

    output_lines.append(f"Загрузка в S3 (bucket={bucket}, key={s3_key})...")
    s3 = _s3_client()
    with local_path.open("rb") as f:
        body = f.read()
    s3.put_object(Bucket=bucket, Key=s3_key, Body=body, ContentType="application/gzip")
    output_lines.append(f"  S3 upload OK")

    return {
        "output": "\n".join(output_lines),
        "result": {
            "filename": filename,
            "local_path": str(local_path),
            "s3_bucket": bucket,
            "s3_key": s3_key,
            "size_bytes": size,
            "size_mb": round(size / 2**20, 2),
            "created_at": ts,
        },
    }


@register_handler("list_backups")
def run_list_backups(params: dict) -> dict:
    """Список бэкапов: локальные файлы + S3 объекты."""
    local: list[dict] = []
    if BACKUP_DIR.exists():
        for p in sorted(BACKUP_DIR.glob("db-*.sql.gz"), reverse=True):
            stat = p.stat()
            local.append({
                "filename": p.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })

    s3_items: list[dict] = []
    bucket = os.environ.get("AWS_BACKUP_BUCKET_NAME") or os.environ["AWS_STORAGE_BUCKET_NAME"]
    try:
        s3 = _s3_client()
        resp = s3.list_objects_v2(Bucket=bucket, Prefix="db-backups/")
        for obj in resp.get("Contents", []):
            s3_items.append({
                "key": obj["Key"],
                "filename": obj["Key"].rsplit("/", 1)[-1],
                "size_bytes": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            })
        s3_items.sort(key=lambda x: x["last_modified"], reverse=True)
    except Exception as e:
        s3_items = [{"error": str(e)}]

    output_lines = [
        f"Локальные ({len(local)}):",
        *[f"  {x['filename']:<32} {x['size_bytes']:>10,} байт" for x in local[:20]],
        "",
        f"S3 ({len(s3_items)}, bucket={bucket}):",
        *[
            f"  {x.get('filename') or x.get('error', '?'):<32} "
            + (f"{x.get('size_bytes', 0):>10,} байт" if "size_bytes" in x else "")
            for x in s3_items[:20]
        ],
    ]
    return {
        "output": "\n".join(output_lines),
        "result": {"local": local, "s3": s3_items, "bucket": bucket},
    }
