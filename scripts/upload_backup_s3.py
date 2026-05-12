#!/usr/bin/env python3
"""Upload a backup file to Beget S3 bucket (используется в backup_db.sh).

Usage: python upload_backup_s3.py /path/to/backup.sql.gz

Если заданы AWS_BACKUP_* — использует их (отдельный бакет/ключи для бэкапов),
иначе fallback на AWS_* (media-бакет, префикс db-backups/).
Beget Cloud валится на boto3 upload_fileobj/put_object (XAmzContentSHA256Mismatch) —
загружаем через pre-signed URL + requests.put.
"""
import os
import sys
from pathlib import Path

import boto3
import requests
from botocore.client import Config


def main(local_path: str) -> None:
    p = Path(local_path)
    if not p.exists():
        print(f"[ERROR] File not found: {p}", file=sys.stderr)
        sys.exit(1)

    bucket = os.environ.get("AWS_BACKUP_BUCKET_NAME") or os.environ["AWS_STORAGE_BUCKET_NAME"]
    endpoint = os.environ.get("AWS_BACKUP_S3_BASE_URL") or os.environ["AWS_S3_BASE_URL"]
    access = os.environ.get("AWS_BACKUP_ACCESS_KEY_ID") or os.environ["AWS_ACCESS_KEY_ID"]
    secret = os.environ.get("AWS_BACKUP_SECRET_ACCESS_KEY") or os.environ["AWS_SECRET_ACCESS_KEY"]
    key = f"db-backups/{p.name}"

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=os.environ.get("AWS_S3_REGION_NAME", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            s3={"payload_signing_enabled": False, "addressing_style": "path"},
        ),
    )

    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": "application/gzip"},
        ExpiresIn=600,
    )
    with p.open("rb") as f:
        body = f.read()
    resp = requests.put(url, data=body, headers={"Content-Type": "application/gzip"}, timeout=300)
    if not resp.ok:
        print(f"[ERROR] S3 PUT failed: HTTP {resp.status_code} — {resp.text[:300]}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Uploaded to s3://{bucket}/{key} ({resp.status_code})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: upload_backup_s3.py <local_path>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
