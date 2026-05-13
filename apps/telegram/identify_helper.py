"""
Помощник для модалки «Идентификация»: тянет ФИО/username/телефон из
Telegram через userbot и опционально добавляет пользователя в контакты,
чтобы получить телефон (Telegram отдаёт phone только для контактов или
если пользователь сам его опубликовал).

Вызывается из sync-view: identify_get_telegram_info(telegram_id) →
{"ok": bool, "first_name", "last_name", "username", "phone",
 "phone_via_contact", "error"}.
"""
import asyncio
import logging
import os

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import AddContactRequest, DeleteContactsRequest
from telethon.tl.types import InputUser, InputPeerUser, PeerUser, User as TLUser
from django.conf import settings

logger = logging.getLogger("userbot")


async def _async_identify(telegram_id: int, try_add_contact: bool = True) -> dict:
    session_string = os.getenv("TELEGRAM_SESSION_STRING", "")
    if not session_string:
        return {
            "ok": False,
            "error": "TELEGRAM_SESSION_STRING не задан — userbot недоступен на этом окружении.",
        }

    client = TelegramClient(
        StringSession(session_string),
        api_id=settings.TELEGRAM_API_ID,
        api_hash=settings.TELEGRAM_API_HASH,
    )

    result: dict = {
        "ok": False,
        "first_name": "",
        "last_name": "",
        "username": "",
        "phone": "",
        "phone_via_contact": False,
        "error": "",
    }

    try:
        await client.connect()

        try:
            entity = await client.get_entity(PeerUser(telegram_id))
        except Exception as e:
            result["error"] = f"Не удалось получить пользователя из Telegram: {e}"
            return result

        if not isinstance(entity, TLUser):
            result["error"] = "Telegram-объект не является пользователем."
            return result

        result["first_name"] = entity.first_name or ""
        result["last_name"] = entity.last_name or ""
        result["username"] = entity.username or ""
        result["phone"] = f"+{entity.phone}" if entity.phone else ""
        result["ok"] = True

        if result["phone"] or not try_add_contact:
            return result

        # Телефона нет — пробуем добавить в контакты, чтобы Telegram отдал номер.
        try:
            access_hash = getattr(entity, "access_hash", 0) or 0
            await client(AddContactRequest(
                id=InputUser(user_id=entity.id, access_hash=access_hash),
                first_name=result["first_name"] or "client",
                last_name=result["last_name"] or "",
                phone="",
                add_phone_privacy_exception=False,
            ))
            # Перечитываем — phone мог появиться.
            entity2 = await client.get_entity(PeerUser(telegram_id))
            if isinstance(entity2, TLUser) and entity2.phone:
                result["phone"] = f"+{entity2.phone}"
                result["phone_via_contact"] = True
        except Exception as e:
            logger.info(f"AddContactRequest для {telegram_id} не помог: {e}")

        return result

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def identify_get_telegram_info(telegram_id: int, try_add_contact: bool = True) -> dict:
    """Sync-обёртка. Запускает новый event loop (как telegram_sender)."""
    if not telegram_id:
        return {"ok": False, "error": "У клиента не задан telegram_id."}
    try:
        return asyncio.run(_async_identify(int(telegram_id), try_add_contact=try_add_contact))
    except Exception as e:
        logger.exception(f"identify_get_telegram_info({telegram_id}) failed: {e}")
        return {"ok": False, "error": str(e)}
