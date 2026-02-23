import logging
from telethon import TelegramClient, events
from telethon.tl.types import UpdateReadHistoryInbox, User, PeerUser
from telethon.tl.functions.messages import GetHistoryRequest
from django.conf import settings
from asgiref.sync import sync_to_async
from apps.crm.models import Message, Client
from django.utils import timezone

logger = logging.getLogger(__name__)

client = TelegramClient(
    'userbot_session',
    settings.TELEGRAM_API_ID,
    settings.TELEGRAM_API_HASH
)


async def get_user_phone(user_id: int) -> str | None:
    """Получить номер телефона пользователя по его ID"""
    try:
        user = await client.get_entity(PeerUser(user_id))
        if isinstance(user, User) and user.phone:
            return f"+{user.phone}"
    except Exception as e:
        logger.warning(f"Could not get phone for user {user_id}: {e}")
    return None


async def import_message_history(telegram_id: int, limit: int = 100):
    """
    Импортирует историю сообщений с клиентом.
    Полезно для первичной загрузки или синхронизации.
    """
    try:
        # Получаем клиента из БД
        db_client = await sync_to_async(
            Client.objects.filter(telegram_id=telegram_id).first
        )()
        
        if not db_client:
            logger.warning(f"Client with telegram_id={telegram_id} not found in DB")
            return
        
        # Получаем историю из Telegram
        peer = await client.get_entity(PeerUser(telegram_id))
        history = await client(GetHistoryRequest(
            peer=peer,
            limit=limit,
            offset_date=None,
            offset_id=0,
            max_id=0,
            min_id=0,
            add_offset=0,
            hash=0
        ))
        
        imported_count = 0
        for msg in history.messages:
            if not msg.message:  # пропускаем сервисные сообщения
                continue
            
            # Проверяем, есть ли уже в БД
            exists = await sync_to_async(
                Message.objects.filter(telegram_message_id=msg.id).exists
            )()
            
            if exists:
                continue
            
            # Определяем направление
            direction = "outgoing" if msg.out else "incoming"
            
            # Создаём в БД
            await sync_to_async(Message.objects.create)(
                client=db_client,
                employee=None,
                content=msg.message,
                message_type="text",
                direction=direction,
                telegram_message_id=msg.id,
                is_sent=True,
                is_read=True if direction == "incoming" else False,
                created_at=msg.date,
            )
            imported_count += 1
        
        logger.info(f"Imported {imported_count} messages for client {telegram_id}")
        
    except Exception as e:
        logger.exception(f"Error importing history for {telegram_id}: {e}")


async def start_userbot():
    """Запускает userbot для отслеживания прочтений и других событий"""
    await client.start(phone=settings.TELEGRAM_PHONE)
    logger.info("✅ Userbot started and connected")
    
    # ========== Обработчик прочтений ==========
    @client.on(events.Raw(types=[UpdateReadHistoryInbox]))
    async def handle_read(event):
        """Когда клиент прочитал наши сообщения"""
        try:
            peer = event.peer
            max_id = event.max_id
            
            if hasattr(peer, 'user_id'):
                telegram_id = peer.user_id
            else:
                return
            
            # Помечаем прочитанными
            updated = await sync_to_async(
                Message.objects.filter(
                    client__telegram_id=telegram_id,
                    direction="outgoing",
                    telegram_message_id__lte=max_id,
                    is_read=False
                ).update
            )(is_read=True, read_at=timezone.now())
            
            if updated > 0:
                logger.info(f"📖 Marked {updated} messages as read for client {telegram_id}")
                
                # Обновляем UI через WebSocket
                from apps.realtime.utils import push_chat_message
                messages = await sync_to_async(list)(
                    Message.objects.filter(
                        client__telegram_id=telegram_id,
                        direction="outgoing",
                        telegram_message_id__lte=max_id,
                        is_read=True
                    )[:updated]
                )
                
                for msg in messages:
                    await sync_to_async(push_chat_message)(msg)
            
        except Exception as e:
            logger.exception("Error handling read receipt: %s", e)
    
    # ========== Обработчик новых входящих сообщений ==========
    @client.on(events.NewMessage(incoming=True))
    async def handle_new_message(event):
        """
        Дублирует функционал бота, но через Client API.
        Можно использовать как резервный канал или для более богатой обработки.
        """
        try:
            sender = await event.get_sender()
            if not sender or not hasattr(sender, 'id'):
                return
            
            telegram_id = sender.id
            
            # Получаем или создаём клиента
            db_client = await sync_to_async(
                Client.objects.filter(telegram_id=telegram_id).first
            )()
            
            if not db_client:
                # Автосоздание клиента
                phone = await get_user_phone(telegram_id)
                db_client = await sync_to_async(Client.objects.create)(
                    telegram_id=telegram_id,
                    first_name=sender.first_name or "",
                    last_name=sender.last_name or "",
                    username=sender.username or "",
                    phone=phone or "",
                    status="lead",
                    last_message_at=timezone.now(),
                )
                logger.info(f"✨ Auto-created client {telegram_id} with phone {phone}")
            
            # Обновляем телефон, если его не было
            if not db_client.phone:
                phone = await get_user_phone(telegram_id)
                if phone:
                    db_client.phone = phone
                    await sync_to_async(db_client.save)(update_fields=['phone'])
            
            # Сохраняем сообщение
            msg = await sync_to_async(Message.objects.create)(
                client=db_client,
                content=event.message.text or "",
                message_type="text",
                direction="incoming",
                telegram_message_id=event.message.id,
                is_sent=True,
                is_read=True,  # входящие считаем сразу прочитанными ботом
            )
            
            from apps.realtime.utils import push_chat_message, push_client_toast
            await sync_to_async(push_chat_message)(msg)
            await sync_to_async(push_client_toast)(
                db_client,
                text=f"💬 Новое сообщение от {db_client.first_name or 'клиента'}",
            )
            
        except Exception as e:
            logger.exception("Error in userbot new message handler: %s", e)
    
    logger.info("👂 Userbot is now listening for events...")
    await client.run_until_disconnected()


def run_userbot():
    """Запуск userbot в sync-режиме"""
    import asyncio
    asyncio.run(start_userbot())
