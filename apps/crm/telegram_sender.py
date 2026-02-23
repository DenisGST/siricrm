# apps/crm/telegram_sender.py

from apps.crm.models import Message
from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3


def create_message_and_store_file(*, client, text=None, file=None, employee=None) -> Message:
    """
    Создаёт Message и при наличии файла:
    - заливает файл в S3,
    - создаёт StoredFile,
    - проставляет тип message_type.
    Ничего не отправляет в Telegram.
    """
    stored = None
    file_name = ""
    message_type = "text"

    if file:
        content_type = (file.content_type or "").lower()
        file_name = file.name

        file_bytes = file.read()
        bucket, key = upload_file_to_s3(
            file_bytes,
            prefix="telegram/media",
            filename=file_name,
        )
        file.seek(0)

        stored = StoredFile.objects.create(
            bucket=bucket,
            key=key,
            filename=file_name,
        )

        if content_type.startswith("audio/"):
            message_type = "audio"
        elif content_type.startswith("video/"):
            message_type = "video"
        elif content_type.startswith("image/"):
            message_type = "image"
        else:
            message_type = "document"
    else:
        message_type = "text"

    msg = Message.objects.create(
        client=client,
        employee=employee,
        content=text or "",
        message_type=message_type,
        direction="outgoing",
        telegram_message_id=None,
        file=stored,
        file_url="",          # если используешь, можешь собрать URL из bucket/key
        file_name=file_name or "",
    )
    return msg