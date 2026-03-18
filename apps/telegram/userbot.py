# /var/www/projects/siricrm/apps/telegram/userbot.py

import os
import logging
import asyncio
from telethon import TelegramClient, events
from telethon.tl.types import User, PeerUser
from telethon.errors import FloodWaitError, AuthKeyError, PhoneCodeExpiredError
from telethon.sessions import StringSession
from django.conf import settings
from asgiref.sync import sync_to_async
from apps.crm.models import Message, Client
from django.utils import timezone

logger = logging.getLogger('userbot')

session_string = os.getenv('TELEGRAM_SESSION_STRING', '')

if session_string:
    client = TelegramClient(
        StringSession(session_string),
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH
    )
    logger.info("📝 Userbot initialized with StringSession")
else:
    client = TelegramClient(
        'userbot_session',
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH
    )
    logger.warning("⚠️ Using file session (TELEGRAM_SESSION_STRING not set)")


async def get_user_phone(user_id: int) -> str | None:
    """Получить номер телефона пользователя по его ID."""
    try:
        entity = await client.get_entity(PeerUser(user_id))
        if isinstance(entity, User) and entity.phone:
            return f"+{entity.phone}"
    except Exception as e:
        logger.warning(f"Could not get phone for user {user_id}: {e}")
    return None


async def import_message_history(telegram_id: int, limit: int = 100):
    """Импорт истории сообщений с клиентом."""
    try:
        db_client = await sync_to_async(
            Client.objects.filter(telegram_id=telegram_id).first
        )()
        if not db_client:
            logger.warning(f"Client with telegram_id={telegram_id} not found in DB")
            return

        peer = await client.get_entity(PeerUser(telegram_id))
        history = await client.get_messages(peer, limit=limit)

        imported_count = 0
        for msg in reversed(history):
            if not msg.message:
                continue

            exists = await sync_to_async(
                Message.objects.filter(telegram_message_id=msg.id).exists
            )()
            if exists:
                continue

            direction = "outgoing" if msg.out else "incoming"

            obj = await sync_to_async(Message.objects.create)(
                client=db_client,
                employee=None,
                content=msg.message,
                message_type="text",
                direction=direction,
                channel="telegram",
                telegram_message_id=msg.id,
                is_sent=True,
                is_read=True if direction == "incoming" else False,
                telegram_date=msg.date,
                raw_payload={
                    "channel": "telegram",
                    "message_id": event.message.id,
                    "peer_id": int(telegram_id),
                    "date": event.date.isoformat() if event.date else None,
                    "media": str(type(event.message.media).__name__) if event.message.media else None,
                },
            )

            imported_count += 1

        logger.info(f"Imported {imported_count} messages for client {telegram_id}")

    except Exception as e:
        logger.exception(f"Error importing history for {telegram_id}: {e}")


async def heartbeat_loop():
    """Каждые 30 секунд пишет heartbeat в Redis."""
    from django.core.cache import cache
    logger.info("❤️ Heartbeat loop started")
    while True:
        try:
            logger.info("❤️ Trying to write heartbeat...")  # добавь эту строку
            await sync_to_async(cache.set)("userbot_heartbeat", "ok", timeout=60)
            logger.info("❤️ Heartbeat written to cache")
        except Exception as e:
            logger.warning(f"Heartbeat cache error: {e}")
        await asyncio.sleep(30)


async def keep_connected():
    """Поддерживает подключение к Telegram."""
    while True:
        try:
            await client.run_until_disconnected()
            break
        except (ConnectionError, OSError) as e:
            logger.warning(f"⚠️ Connection lost: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)
            try:
                if not client.is_connected():
                    await client.connect()
            except Exception as conn_err:
                logger.error(f"Failed to reconnect: {conn_err}")
                await asyncio.sleep(30)
        except Exception as e:
            logger.exception(f"❌ Critical error in event loop: {e}")
            break


async def start_userbot():
    """Запускает userbot для отслеживания прочтений и входящих сообщений."""
    MAX_RETRIES = 10
    retry_count = 0
    base_delay = 60

    while retry_count < MAX_RETRIES:
        try:
            await client.start(phone=settings.TELEGRAM_PHONE)
            client.flood_sleep_threshold = 60
            logger.info("✅ Userbot started and connected")
            break

        except FloodWaitError as e:
            retry_count += 1
            wait_time = e.seconds if hasattr(e, 'seconds') else base_delay * (2 ** retry_count)
            logger.warning(
                f"⏳ FloodWaitError: Telegram requires wait of {wait_time}s. "
                f"Retry {retry_count}/{MAX_RETRIES} in {wait_time}s..."
            )
            if retry_count >= MAX_RETRIES:
                logger.error("❌ Max retries reached. Stopping userbot.")
                raise
            await asyncio.sleep(wait_time)

        except (AuthKeyError, PhoneCodeExpiredError) as e:
            logger.error(
                f"❌ Auth error: {e}. Session may be invalid. "
                "Delete 'userbot_session.session' and re-authorize."
            )
            raise

        except EOFError:
            retry_count += 1
            delay = base_delay * (2 ** (retry_count - 1))
            logger.warning(
                f"⏳ EOFError (no interactive terminal). "
                f"Retry {retry_count}/{MAX_RETRIES} in {delay}s..."
            )
            if retry_count >= MAX_RETRIES:
                logger.error(
                    "❌ Max retries reached. Run 'python manage.py run_userbot' "
                    "locally to authorize first."
                )
                raise
            await asyncio.sleep(delay)

        except Exception as e:
            retry_count += 1
            delay = base_delay * (2 ** (retry_count - 1))
            logger.exception(
                f"❌ Unexpected error during userbot start: {e}. "
                f"Retry {retry_count}/{MAX_RETRIES} in {delay}s..."
            )
            if retry_count >= MAX_RETRIES:
                logger.error("❌ Max retries reached. Stopping userbot.")
                raise
            await asyncio.sleep(delay)

    # ========= Обработчик прочтений =========
    @client.on(events.Raw)
    async def handle_read(event):
        """Обработка прочтений."""
        from telethon.tl.types import UpdateReadHistoryInbox

        if not isinstance(event, UpdateReadHistoryInbox):
            return

        try:
            peer = event.peer
            max_id = event.max_id

            telegram_id = getattr(peer, "user_id", None)
            if not telegram_id:
                return

            updated = await sync_to_async(
                Message.objects.filter(
                    client__telegram_id=telegram_id,
                    direction="outgoing",
                    telegram_message_id__lte=max_id,
                    is_read=False,
                ).update
            )(is_read=True, read_at=timezone.now())

            if updated > 0:
                logger.info(f"📖 Marked {updated} messages as read for client {telegram_id}")

                from apps.realtime.utils import push_chat_message

                messages = await sync_to_async(list)(
                    Message.objects.filter(
                        client__telegram_id=telegram_id,
                        direction="outgoing",
                        telegram_message_id__lte=max_id,
                        is_read=True,
                    ).order_by("-telegram_message_id")[:updated]
                )

                for msg in messages:
                    await sync_to_async(push_chat_message)(msg)

        except Exception as e:
            from django.db import connection
            connection.close()
            logger.exception("Error handling read receipt: %s", e)

    # ========= Обработчик новых входящих сообщений =========
    @client.on(events.NewMessage(incoming=True))
    async def handle_new_message(event):
        """Обрабатываем только личные сообщения от пользователей."""
        try:
            sender = await event.get_sender()
            if not isinstance(sender, User):
                return

            telegram_id = sender.id

            db_client = await sync_to_async(
                Client.objects.filter(telegram_id=telegram_id).first
            )()

            if not db_client:
                phone = await get_user_phone(telegram_id)

                db_client, created = await sync_to_async(Client.objects.get_or_create)(
                    telegram_id=telegram_id,
                    defaults={
                        'first_name': sender.first_name or "",
                        'last_name': sender.last_name or "",
                        'username': sender.username or "",
                        'phone': phone or "",
                        'status': "lead",
                        'last_message_at': timezone.now(),
                    }
                )

                if created:
                    logger.info(f"✨ Created new client {telegram_id} with phone {phone}")
                else:
                    logger.info(f"✅ Found existing client {telegram_id}")

            db_client.last_message_at = timezone.now()
            await sync_to_async(db_client.save)(update_fields=["last_message_at"])

            # ========= ОБРАБОТКА МЕДИА =========
            message_type = "text"
            file_data = None
            file_name = ""
            content = event.message.text or ""

            if event.message.media:
                from telethon.tl.types import (
                    MessageMediaDocument, MessageMediaPhoto,
                    DocumentAttributeAudio, DocumentAttributeFilename
                )
                from apps.files.s3_utils import upload_file_to_s3
                from apps.files.models import StoredFile

                media = event.message.media

                if isinstance(media, MessageMediaDocument):
                    doc = media.document

                    is_voice = False
                    is_audio = False
                    original_filename = "file"

                    for attr in doc.attributes:
                        if isinstance(attr, DocumentAttributeAudio):
                            if attr.voice:
                                is_voice = True
                                message_type = "voice"
                                original_filename = "voice.ogg"
                            else:
                                is_audio = True
                                message_type = "audio"
                                original_filename = attr.title or "audio.mp3"
                        elif isinstance(attr, DocumentAttributeFilename):
                            original_filename = attr.file_name

                    if not is_voice and not is_audio:
                        mime = doc.mime_type or ""
                        if mime.startswith("video/"):
                            message_type = "video"
                            original_filename = "video.mp4"
                        elif mime.startswith("image/"):
                            message_type = "image"
                            original_filename = "image.jpg"
                        else:
                            message_type = "document"

                    file_bytes = await client.download_media(event.message, bytes)

                    if file_bytes:
                        bucket, key = await sync_to_async(upload_file_to_s3)(
                            file_bytes,
                            prefix="telegram/media",
                            filename=original_filename
                        )

                        stored_file = await sync_to_async(StoredFile.objects.create)(
                            bucket=bucket,
                            key=key,
                            filename=original_filename,
                            content_type=doc.mime_type or "application/octet-stream",
                            size=len(file_bytes)
                        )

                        file_data = stored_file
                        file_name = original_filename
                        logger.info(f"📎 Downloaded {message_type} file: {original_filename} ({len(file_bytes)} bytes)")

                elif isinstance(media, MessageMediaPhoto):
                    message_type = "image"
                    original_filename = "photo.jpg"

                    file_bytes = await client.download_media(event.message, bytes)

                    if file_bytes:
                        bucket, key = await sync_to_async(upload_file_to_s3)(
                            file_bytes,
                            prefix="telegram/media",
                            filename=original_filename
                        )

                        stored_file = await sync_to_async(StoredFile.objects.create)(
                            bucket=bucket,
                            key=key,
                            filename=original_filename,
                            content_type="image/jpeg",
                            size=len(file_bytes)
                        )

                        file_data = stored_file
                        file_name = original_filename
                        logger.info(f"🖼️ Downloaded image: {len(file_bytes)} bytes")

            msg = await sync_to_async(Message.objects.create)(
                client=db_client,
                content=content,
                message_type=message_type,
                direction="incoming",
                channel="telegram",
                telegram_message_id=event.message.id,
                telegram_date=event.date,
                file=file_data,
                file_name=file_name,
                is_sent=True,
                is_read=True,
                raw_payload={
                    "channel": "telegram",
                    "message_id": event.message.id,
                    "peer_id": int(telegram_id),
                    "date": event.date.isoformat() if event.date else None,
                    "media": type(event.message.media).__name__ if event.message.media else None,
                },
            )

            logger.info(f"💬 Incoming {message_type} from {telegram_id}: {content[:50] if content else file_name}")

            from apps.realtime.utils import push_chat_message, push_client_toast

            await sync_to_async(push_chat_message)(msg)

            preview_source = content or file_name or ""
            preview = (preview_source[:15] + "…") if len(preview_source) > 15 else preview_source

            toast_text = (
                f"💬 {preview} — новое сообщение от "
                f"{db_client.first_name or db_client.username or 'клиента'}"
            )

            await sync_to_async(push_client_toast)(
                db_client,
                text=toast_text,
            )

        except Exception as e:
            from django.db import connection
            logger.exception("Error in userbot new message handler: %s", e)

    logger.info("👂 Userbot is now listening for events...")

    # Запускаем heartbeat и подключение параллельно
    await asyncio.gather(
        heartbeat_loop(),
        keep_connected(),
    )


def run_userbot():
    """Запуск userbot в sync-режиме."""
    asyncio.run(start_userbot())
