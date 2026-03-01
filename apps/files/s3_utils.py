# apps/files/s3_utils.py

import io
import uuid
import logging
import boto3
from django.conf import settings
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Конфигурация для S3-совместимых хранилищ
config = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required"
)

# S3 клиент
s3_client = boto3.client(
    "s3",
    endpoint_url=settings.AWS_S3_BASE_URL,
    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    region_name=settings.AWS_S3_REGION_NAME,
    config=config,
)


def upload_file_to_s3(
    file_bytes: bytes, 
    *, 
    prefix: str = "uploads", 
    filename: str = "file.bin",
    content_type: str = None
) -> tuple[str, str]:
    """
    Загружает файл в S3 и возвращает (bucket, key).
    
    Args:
        file_bytes: Байты файла
        prefix: Папка внутри бакета, например 'telegram/images'
        filename: Имя файла для определения расширения
        content_type: MIME-тип файла (опционально)
    
    Returns:
        tuple[str, str]: (bucket_name, file_key)
    """
    try:
        # Определяем расширение файла
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        
        # Генерируем уникальный ключ
        key = f"{prefix.rstrip('/')}/{uuid.uuid4()}.{ext}"
        
        # Параметры загрузки
        extra_args = {}
        if content_type:
            extra_args['ContentType'] = content_type
        
        # Загружаем в S3
        s3_client.upload_fileobj(
            io.BytesIO(file_bytes),
            settings.AWS_STORAGE_BUCKET_NAME,
            key,
            ExtraArgs=extra_args if extra_args else None
        )
        
        logger.info(f"✅ Uploaded file to S3: {key}")
        return settings.AWS_STORAGE_BUCKET_NAME, key
        
    except ClientError as e:
        logger.exception(f"❌ S3 upload error for {filename}: {e}")
        raise
    except Exception as e:
        logger.exception(f"❌ Unexpected error uploading {filename}: {e}")
        raise


def download_file_from_s3(bucket: str, key: str) -> bytes:
    """
    Скачивает файл из S3 и возвращает его как bytes.
    
    Args:
        bucket: Имя бакета S3
        key: Ключ (путь) файла в бакете
    
    Returns:
        bytes: Содержимое файла
    """
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        file_bytes = resp["Body"].read()
        
        logger.info(f"✅ Downloaded file from S3: {key} ({len(file_bytes)} bytes)")
        return file_bytes
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        if error_code == 'NoSuchKey':
            logger.error(f"❌ File not found in S3: {key}")
        else:
            logger.exception(f"❌ S3 download error for {key}: {e}")
        raise
    except Exception as e:
        logger.exception(f"❌ Unexpected error downloading {key}: {e}")
        raise


def get_presigned_url(bucket: str, key: str, expiration: int = 3600) -> str:
    """
    Генерирует подписанный URL для доступа к файлу в S3.
    
    Args:
        bucket: Имя бакета S3
        key: Ключ файла
        expiration: Время действия URL в секундах (по умолчанию 1 час)
    
    Returns:
        str: Подписанный URL
    """
    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=expiration
        )
        logger.info(f"✅ Generated presigned URL for {key} (expires in {expiration}s)")
        return url
        
    except ClientError as e:
        logger.exception(f"❌ Error generating presigned URL for {key}: {e}")
        raise


def delete_file_from_s3(bucket: str, key: str) -> bool:
    """
    Удаляет файл из S3.
    
    Args:
        bucket: Имя бакета S3
        key: Ключ файла
    
    Returns:
        bool: True если успешно удалено
    """
    try:
        s3_client.delete_object(Bucket=bucket, Key=key)
        logger.info(f"✅ Deleted file from S3: {key}")
        return True
        
    except ClientError as e:
        logger.exception(f"❌ Error deleting file {key}: {e}")
        return False


def file_exists_in_s3(bucket: str, key: str) -> bool:
    """
    Проверяет существование файла в S3.
    
    Args:
        bucket: Имя бакета S3
        key: Ключ файла
    
    Returns:
        bool: True если файл существует
    """
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == '404':
            return False
        logger.exception(f"❌ Error checking file existence {key}: {e}")
        return False
