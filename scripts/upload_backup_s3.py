#!/usr/bin/env python3
"""Upload a backup file to Beget S3 bucket.

Usage: python upload_backup_s3.py /path/to/backup.sql.gz
"""
import os
import sys
from pathlib import Path

import boto3
from botocore.client import Config


def main(local_path: str) -> None:
    p = Path(local_path)
    if not p.exists():
        print(f"[ERROR] File not found: {p}", file=sys.stderr)
        sys.exit(1)

    bucket = os.environ.get("AWS_BACKUP_BUCKET_NAME") or os.environ["AWS_STORAGE_BUCKET_NAME"]
    endpoint = os.environ["AWS_S3_BASE_URL"]
    key = f"db-backups/{p.name}"

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_S3_REGION_NAME", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            s3={"payload_signing_enabled": False, "addressing_style": "path"},
        ),
    )

    with p.open("rb") as f:
        s3.upload_fileobj(f, bucket, key)

    print(f"[OK] Uploaded to s3://{bucket}/{key}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: upload_backup_s3.py <local_path>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
