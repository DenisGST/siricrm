# apps/crm/telegram_sender.py
import asyncio
import logging
from pathlib import Path

from django.conf import settings

from telegram import Bot
from telegram.error import TelegramError

from apps.crm.models import Client, Message
from apps.core.models import Employee

from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3



logger = logging.getLogger(__name__)


async def _send_text_async(
    *,
    client: Client,
    text: str,
    employee: Employee | None,
) -> None:
    if not client.telegram_id:
        logger.error("Client %s has no telegram_id", client)
        return

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

    try:
        sent_msg = await bot.send_message(
            chat_id=client.telegram_id,
            text=text,
        )

        await asyncio.to_thread(
            Message.objects.create,
            client=client,
            employee=employee,
            content=text,
            message_type="text",
            direction="outgoing",
            telegram_message_id=sent_msg.message_id,
        )
        logger.info(
            "Sent Telegram message to client %s (chat_id=%s, msg_id=%s)",
            client.id,
            client.telegram_id,
            sent_msg.message_id,
        )

    except TelegramError as e:
        logger.exception(
            "TelegramError sending message to chat_id=%s: %s",
            client.telegram_id,
            e,
        )
        await asyncio.to_thread(
            Message.objects.create,
            client=client,
            employee=employee,
            content=text,
            message_type="text",
            direction="outgoing",
        )
    except Exception as e:
        logger.exception(
            "Unexpected error sending message to chat_id=%s: %s",
            client.telegram_id,
            e,
        )
        await asyncio.to_thread(
            Message.objects.create,
            client=client,
            employee=employee,
            content=text,
            message_type="text",
            direction="outgoing",
        )


def send_text_from_crm_sync(
    *,
    client: Client,
    text: str,
    employee: Employee | None = None,
) -> None:
    if not text:
        return

    logger.info(
        "send_text_from_crm_sync: client=%s, chat_id=%s, text=%r",
        client.id,
        client.telegram_id,
        text,
    )

    asyncio.run(
        _send_text_async(client=client, text=text, employee=employee)
    )

async def _send_file_async(
    *,
    client: Client,
    file_bytes: bytes,
    filename: str,
    employee: Employee | None,
):
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

    suffix = Path(filename).suffix.lower()
    is_image = suffix in [".jpg", ".jpeg", ".png", ".gif", ".webp"]

    prefix = "telegram/images" if is_image else "telegram/docs"

    bucket, key = upload_file_to_s3(
        file_bytes,
        prefix=prefix,
        filename=filename,
    )

    stored_file = await asyncio.to_thread(
        StoredFile.objects.create,
        bucket=bucket,
        key=key,
        filename=filename,
    )

    if is_image:
        sent_msg = await bot.send_photo(
            chat_id=client.telegram_id,
            photo=file_bytes,
        )
        msg_type = "image"
        content = "Изображение от сотрудника"
    else:
        sent_msg = await bot.send_document(
            chat_id=client.telegram_id,
            document=file_bytes,
            filename=filename,
        )
        msg_type = "document"
        content = f"Файл: {filename}"

    await asyncio.to_thread(
        Message.objects.create,
        client=client,
        employee=employee,
        message_type=msg_type,
        content=content,
        telegram_message_id=sent_msg.message_id,
        file=stored_file,
        direction="outgoing",
    )

def send_from_crm_sync(
    *,
    client: Client,
    text: str | None,
    file,
    employee: Employee | None = None,
) -> None:
    if not client.telegram_id:
        logger.error("Client %s has no telegram_id", client)
        return

    try:
        if file:
            filename = getattr(file, "name", "file")
            file_bytes = file.read()
            asyncio.run(
                _send_file_async(
                    client=client,
                    file_bytes=file_bytes,
                    filename=filename,
                    employee=employee,
                )
            )
        if text:
            asyncio.run(
                _send_text_async(
                    client=client,
                    text=text,
                    employee=employee,
                )
            )
    except TelegramError as e:
        logger.exception(
            "TelegramError sending to chat_id=%s: %s",
            client.telegram_id,
            e,
        )
    except Exception as e:
        logger.exception(
            "Unexpected error sending to chat_id=%s: %s",
            client.telegram_id,
            e,
        )