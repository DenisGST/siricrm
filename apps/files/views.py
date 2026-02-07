from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth.decorators import login_required

import boto3
from django.conf import settings

from .models import StoredFile

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

@login_required
def download_file(request, file_id):
    stored = get_object_or_404(StoredFile, pk=file_id)

    # TODO: здесь можно проверить права доступа по связанным сообщениям

    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": stored.bucket, "Key": stored.key},
        ExpiresIn=300,
    )
    return redirect(url)
