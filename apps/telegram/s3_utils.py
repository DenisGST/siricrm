import uuid
import boto3
from django.conf import settings

import os
import logging
from botocore.config import Config


logger = logging.getLogger(__name__)

config = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required"
)

def upload_telegram_file_to_s3(file_bytes: bytes, prefix: str, filename: str | None = None) -> str:
    """
    Загружает байты в S3 и возвращает публичный URL.
    prefix: подкаталог в бакете, напр. "telegram/images" или "telegram/docs".
    """
    
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_BASE_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        config=config,
        
    )

    if not filename:
        filename = uuid.uuid4().hex

    key = f"{prefix.rstrip('/')}/{filename}"

    s3.put_object(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Key=key,
        Body=file_bytes,

    )

    return f"{settings.AWS_S3_BASE_URL}/{key}"