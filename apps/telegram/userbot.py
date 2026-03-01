import logging
from telethon import TelegramClient, events
from telethon.tl.types import User, PeerUser
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
    """
    Получить номер телефона пользователя по его ID.
    ВАЖНО: вызывать ТОЛЬКО при первом входящем сообщении,
    дальше использовать значение из БД.
    """
    try:
        entity = await client.get_entity(PeerUser(user_id))
        if isinstance(entity, User) and entity.phone:
            return f"+{entity.phone}"
    except Exception as e:
        logger.warning(f"Could not get phone for user {user_id}: {e}")
    return None


async def import_message_history(telegram_id: int, limit: int = 100):
    """
    Импорт истории сообщений с клиентом.
    ОСТАВЛЕНО как утилита под ручную кнопку в CRM.
    НЕ вызывать автоматически по всем клиентам.
    """
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
        for msg in history:
            if not msg.message:
                continue

            exists = await sync_to_async(
                Message.objects.filter(telegram_message_id=msg.id).exists
            )()
            if exists:
                continue

            direction = "outgoing" if msg.out else "incoming"

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
    """Запускает userbot для отслеживания прочтений и входящих сообщений."""
    await client.start(phone=settings.TELEGRAM_PHONE)
    logger.info("✅ Userbot started and connected")

    # ========= Обработчик прочтений =========
    @client.on(events.Raw)
    async def handle_read(event):
        """
        Аккуратная обработка прочтений.
        Проверяем тип события, чтобы не ловить лишнее.
        """
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
                logger.info(
                    f"📖 Marked {updated} messages as read for client {telegram_id}"
                )

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
            logger.exception("Error handling read receipt: %s", e)

    # ========= Обработчик новых входящих сообщений =========
    @client.on(events.NewMessage(incoming=True))
    async def handle_new_message(event):
        """
        Обрабатываем только личные сообщения от пользователей.
        Телефон запрашиваем только при первом входящем.
        """
        try:
            sender = await event.get_sender()
            # Игнорируем каналы/чатов, у которых нет id как у User
            if not isinstance(sender, User):
                return

            telegram_id = sender.id

            # Пытаемся найти клиента в БД
            db_client = await sync_to_async(
                Client.objects.filter(telegram_id=telegram_id).first
            )()

            if not db_client:
                # Первый входящий: создаём клиента и ОДИН раз запрашиваем телефон
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
                logger.info(
                    f"✨ Auto-created client {telegram_id} "
                    f"with phone {phone}"
                )
            else:
                # Обновляем last_message_at
                db_client.last_message_at = timezone.now()
                await sync_to_async(db_client.save)(update_fields=["last_message_at"])

                # Если телефона нет, пробуем ОДИН раз добить
                if not db_client.phone:
                    phone = await get_user_phone(telegram_id)
                    if phone:
                        db_client.phone = phone
                        await sync_to_async(db_client.save)(update_fields=["phone"])
                        logger.info(
                            f"📱 Updated phone for client {telegram_id} to {phone}"
                        )

            # Сохраняем входящее сообщение
            msg = await sync_to_async(Message.objects.create)(
                client=db_client,
                content=event.message.text or "",
                message_type="text",
                direction="incoming",
                telegram_message_id=event.message.id,
                is_sent=True,
                is_read=True,
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
    """Запуск userbot в sync-режиме."""
    import asyncio

    asyncio.run(start_userbot())
