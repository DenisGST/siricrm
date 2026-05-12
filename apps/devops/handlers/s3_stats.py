"""S3-stats handler: статистика по бакетам, доступным с этого сервера.

Запускается в devops-runner. Видит только те бакеты, ключи к которым есть в env
этого окружения (на dev — `1464bbae4a12-siridev-s3`; на prod — media-бакет +
backup-бакет по отдельным `AWS_BACKUP_*`). Чтобы увидеть prod-бакеты с dev-панели,
запусти это действие на PROD-окружении (оно уйдёт на prod-агента).
"""
import os

import boto3
from botocore.client import Config

from apps.devops.tasks import register_handler


# Защита от очень больших бакетов — не хотим висеть в Celery вечно.
_MAX_OBJECTS = 100_000


def _client(access: str, secret: str, endpoint: str, region: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=region or "us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"payload_signing_enabled": False, "addressing_style": "path"},
        ),
    )


def _bucket_stats(client, bucket: str) -> dict:
    total_size = 0
    count = 0
    truncated = False
    prefixes: dict[str, dict] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            if count >= _MAX_OBJECTS:
                truncated = True
                break
            sz = obj["Size"]
            total_size += sz
            count += 1
            key = obj["Key"]
            top = (key.split("/", 1)[0] + "/") if "/" in key else "(корень)"
            p = prefixes.setdefault(top, {"size": 0, "count": 0})
            p["size"] += sz
            p["count"] += 1
        if truncated:
            break
    return {
        "bucket": bucket,
        "objects": count,
        "truncated": truncated,
        "size_bytes": total_size,
        "size_mb": round(total_size / 2**20, 2),
        "size_gb": round(total_size / 2**30, 3),
        "prefixes": sorted(
            (
                {"prefix": k, "count": v["count"], "size_bytes": v["size"],
                 "size_mb": round(v["size"] / 2**20, 2)}
                for k, v in prefixes.items()
            ),
            key=lambda x: x["size_bytes"], reverse=True,
        ),
    }


def _sources() -> list[tuple[str, tuple]]:
    """(роль, (access, secret, endpoint, region, bucket)) из env."""
    main = (
        os.environ.get("AWS_ACCESS_KEY_ID"),
        os.environ.get("AWS_SECRET_ACCESS_KEY"),
        os.environ.get("AWS_S3_BASE_URL"),
        os.environ.get("AWS_S3_REGION_NAME"),
        os.environ.get("AWS_STORAGE_BUCKET_NAME"),
    )
    backup = (
        os.environ.get("AWS_BACKUP_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID"),
        os.environ.get("AWS_BACKUP_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY"),
        os.environ.get("AWS_BACKUP_S3_BASE_URL") or os.environ.get("AWS_S3_BASE_URL"),
        os.environ.get("AWS_S3_REGION_NAME"),
        os.environ.get("AWS_BACKUP_BUCKET_NAME") or os.environ.get("AWS_STORAGE_BUCKET_NAME"),
    )
    return [("медиа", main), ("бэкапы", backup)]


@register_handler("s3_stats")
def run_s3_stats(params: dict) -> dict:
    seen: set[str] = set()
    buckets_out: list[dict] = []
    errors: list[dict] = []

    for role, (access, secret, endpoint, region, bucket) in _sources():
        if not bucket or not access:
            continue
        if bucket in seen:  # тот же бакет под другой ролью — не дублируем
            for b in buckets_out:
                if b["bucket"] == bucket and role not in b["roles"]:
                    b["roles"].append(role)
            continue
        seen.add(bucket)
        try:
            st = _bucket_stats(_client(access, secret, endpoint, region), bucket)
            st["roles"] = [role]
            st["endpoint"] = endpoint
            buckets_out.append(st)
        except Exception as e:
            errors.append({"bucket": bucket, "error": str(e)})

    env_name = os.environ.get("DJANGO_ENV", "?")
    lines = [f"Окружение: {env_name}", ""]
    for b in buckets_out:
        cap = " (показаны первые %s)" % f"{_MAX_OBJECTS:,}" if b["truncated"] else ""
        lines.append(f"Бакет {b['bucket']}  [{'/'.join(b['roles'])}]{cap}")
        lines.append(f"  Объектов: {b['objects']:,}   Размер: {b['size_mb']:,} MB ({b['size_gb']} GB)")
        for p in b["prefixes"][:20]:
            lines.append(f"    {p['prefix']:<28} {p['count']:>7,} об.  {p['size_mb']:>11,} MB")
        lines.append("")
    for e in errors:
        lines.append(f"Бакет {e['bucket']}: ОШИБКА {e['error']}")
    if not buckets_out and not errors:
        lines.append("Ни одного бакета не настроено в env этого окружения.")

    return {
        "output": "\n".join(lines).strip(),
        "result": {"env": env_name, "buckets": buckets_out, "errors": errors},
    }
