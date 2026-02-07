import io
import uuid
import boto3


from django.conf import settings

from botocore.config import Config
config = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required"
)

s3_client = boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_BASE_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        config=config,
        
    )

def upload_file_to_s3(file_bytes: bytes, *, prefix: str, filename: str) -> tuple[str, str]:
    """
    Загружает файл в S3 и возвращает (bucket, key).
    prefix — папка внутри бакета, например 'telegram/images'.
    """
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    key = f"{prefix.rstrip('/')}/{uuid.uuid4()}.{ext}"

    s3_client.upload_fileobj(
        io.BytesIO(file_bytes),
        settings.AWS_STORAGE_BUCKET_NAME,
        key,
    )

    return settings.AWS_STORAGE_BUCKET_NAME, key
