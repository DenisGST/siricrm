# apps/telegram/telegram_sender.py

import logging
import io
from telethon import TelegramClient
from telethon.tl.types import PeerUser, InputMediaUploadedDocument, DocumentAttributeFilename, DocumentAttributeAudio
from django.conf import settings
from apps.crm.models import Message
from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3

logger = logging.getLogger(__name__)

# Используем тот же клиент, что и в userbot
client = TelegramClient(
    'userbot_session',
    settings.TELEGRAM_API_ID,
    settings.TELEGRAM_API_HASH
)


async def ensure_connected():
    """Проверяет подключение к Telegram"""
    if not client.is_connected():
        await client.connect()
        if not await client.is_user_authorized():
            await client.start(phone=settings.TELEGRAM_PHONE)


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
    try:
        await ensure_connected()
        
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
            # Голосовое сообщение
            message = await client.send_file(
                peer,
                file_to_send,
                voice_note=True,
                caption=text or ""
            )
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
        file_name = file.name
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
        )
        
        # Определяем тип сообщения
        if content_type.startswith("audio/"):
            # Проверяем, голосовое ли сообщение (обычно ogg или opus)
            if "ogg" in content_type or "opus" in content_type:
                message_type = "voice"
            else:
                message_type = "audio"
        elif content_type.startswith("video/"):
            message_type = "video"
        elif content_type.startswith("image/"):
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
        telegram_message_id=None,  # Заполнится после отправки
        file=stored,
        file_url="",  # Можно собрать URL из bucket/key если нужно
        file_name=file_name or "",
        is_sent=False,  # Будет True после успешной отправки
        is_delivered=False,  # Отслеживается через userbot
        is_read=False,  # Отслеживается через userbot
    )
    
    logger.info(f"📝 Created message {msg.id} for client {client.id}, type={message_type}")
    return msg
