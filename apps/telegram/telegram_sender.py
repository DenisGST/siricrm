# apps/telegram/telegram_sender.py

import logging
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser, DocumentAttributeFilename, DocumentAttributeAudio
from django.conf import settings
from apps.crm.models import Message
from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3

logger = logging.getLogger(__name__)


async def get_telegram_client():
    """
    Создаёт новый Telegram клиент для текущего event loop.
    Вызывается при каждой отправке сообщения.
    """
    session_string = os.getenv('TELEGRAM_SESSION_STRING', '')
    
    if not session_string:
        raise ValueError("❌ TELEGRAM_SESSION_STRING not found in environment")
    
    client = TelegramClient(
        StringSession(session_string),
        api_id=settings.TELEGRAM_API_ID,
        api_hash=settings.TELEGRAM_API_HASH
    )
    
    await client.connect()
    logger.info("✅ Telegram client connected")
    
    return client


async def send_telegram_message(
    telegram_id: int,
    text: str = None,
    file_path: str = None,
    file_bytes: bytes = None,
    file_name: str = None,
    message_type: str = "text",
    parse_mode: str = 'html'
) -> dict:
    """
    Отправка сообщения через userbot с поддержкой медиа
    
    Args:
        telegram_id: ID получателя в Telegram
        text: Текст сообщения
        file_path: Путь к файлу (опционально)
        file_bytes: Байты файла (опционально, альтернатива file_path)
        file_name: Имя файла для отображения
        message_type: Тип сообщения (text, image, video, audio, document, voice)
        parse_mode: Режим парсинга ('html' или 'markdown')
    
    Returns:
        dict с полями success, message_id, error
    """
    client = None
    try:
        # Создаём новый клиент для текущего event loop
        client = await get_telegram_client()
        
        peer = PeerUser(telegram_id)
        
        # Отправка текстового сообщения
        if message_type == "text" or (not file_path and not file_bytes):
            message = await client.send_message(
                peer,
                text or "",
                parse_mode='html' if parse_mode == 'html' else 'md'
            )
            logger.info(f"✅ Text message sent to {telegram_id}, message_id={message.id}")
            return {
                'success': True,
                'message_id': message.id,
                'error': None
            }
        
        # Отправка медиа
        file_to_send = file_bytes if file_bytes else file_path
        
        if message_type == "voice":
            # Голосовое сообщение — загружаем файл и отправляем с voice=True
            from telethon.tl.functions.messages import SendMediaRequest
            from telethon.tl.types import InputMediaUploadedDocument, DocumentAttributeAudio
            
            # Загружаем файл в Telegram
            uploaded_file = await client.upload_file(file_to_send)
            
            # Отправляем как голосовое сообщение
            result = await client(SendMediaRequest(
                peer=peer,
                media=InputMediaUploadedDocument(
                    file=uploaded_file,
                    mime_type='audio/ogg',
                    attributes=[DocumentAttributeAudio(
                        duration=0,
                        voice=True
                    )]
                ),
                message=text or "",
                random_id=int.from_bytes(os.urandom(8), 'big', signed=True)
            ))
            
            message = result.updates[0].message
            logger.info(f"🎤 Voice message sent to {telegram_id}, message_id={message.id}")


        elif message_type == "audio":
            # Аудиофайл
            message = await client.send_file(
                peer,
                file_to_send,
                caption=text or "",
                attributes=[DocumentAttributeAudio(
                    duration=0,
                    voice=False,
                    title=file_name or "Audio"
                )]
            )
            logger.info(f"🎵 Audio sent to {telegram_id}, message_id={message.id}")
        
        elif message_type == "image":
            # Изображение
            message = await client.send_file(
                peer,
                file_to_send,
                caption=text or ""
            )
            logger.info(f"🖼️ Image sent to {telegram_id}, message_id={message.id}")
        
        elif message_type == "video":
            # Видео
            message = await client.send_file(
                peer,
                file_to_send,
                caption=text or ""
            )
            logger.info(f"🎬 Video sent to {telegram_id}, message_id={message.id}")
        
        else:
            # Документ
            attributes = []
            if file_name:
                attributes.append(DocumentAttributeFilename(file_name))
            
            message = await client.send_file(
                peer,
                file_to_send,
                caption=text or "",
                attributes=attributes
            )
            logger.info(f"📎 Document sent to {telegram_id}, message_id={message.id}")
        
        return {
            'success': True,
            'message_id': message.id,
            'error': None
        }
    
    except Exception as e:
        logger.exception(f"❌ Failed to send message to {telegram_id}: {e}")
        return {
            'success': False,
            'message_id': None,
            'error': str(e)
        }
    
    finally:
        # Обязательно отключаем клиент
        if client:
            try:
                await client.disconnect()
            except:
                pass


def create_message_and_store_file(*, client, text=None, file=None, employee=None) -> Message:
    """
    Создаёт Message и при наличии файла:
    - заливает файл в S3,
    - создаёт StoredFile,
    - проставляет тип message_type.
    Ничего не отправляет в Telegram (отправка через задачу).
    """
    stored = None
    file_name = ""
    message_type = "text"
    
    if file:
        content_type = (file.content_type or "").lower()
        file_name = file.name or "file"
        file_bytes = file.read()
        
        # Загружаем в S3
        bucket, key = upload_file_to_s3(
            file_bytes,
            prefix="telegram/media",
            filename=file_name,
        )
        
        file.seek(0)
        
        # Создаём StoredFile
        stored = StoredFile.objects.create(
            bucket=bucket,
            key=key,
            filename=file_name,
            content_type=content_type,
            size=len(file_bytes),
        )
        
        # Определяем тип сообщения
        ext = file_name.lower().split('.')[-1] if '.' in file_name else ''
        
        if content_type.startswith("audio/") or ext in ['ogg', 'oga', 'opus', 'mp3', 'wav', 'm4a']:
            # Голосовое сообщение: ogg/opus
            if ext in ['ogg', 'oga', 'opus'] or 'ogg' in content_type or 'opus' in content_type:
                message_type = "voice"
            else:
                # Обычное аудио: mp3, wav, m4a и т.д.
                message_type = "audio"
        elif content_type.startswith("video/") or ext in ['mp4', 'avi', 'mov', 'mkv']:
            message_type = "video"
        elif content_type.startswith("image/") or ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
            message_type = "image"
        else:
            message_type = "document"
    else:
        message_type = "text"
    
    # Создаём сообщение в БД
    msg = Message.objects.create(
        client=client,
        employee=employee,
        content=text or "",
        message_type=message_type,
        direction="outgoing",
        telegram_message_id=None,
        file=stored,
        file_url="",
        file_name=file_name,
        is_sent=False,
        is_delivered=False,
        is_read=False,
    )
    
    logger.info(f"📝 Created message {msg.id} for client {client.id}, type={message_type}, file={file_name}")
    return msg
