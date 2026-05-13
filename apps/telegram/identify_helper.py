"""
Помощник для модалки «Идентификация»: тянет ФИО/username/телефон из
Telegram через userbot и опционально добавляет пользователя в контакты,
чтобы получить телефон (Telegram отдаёт phone только для контактов или
если пользователь сам его опубликовал).

Дополнительно: склеивает last_name + first_name (так как в Telegram
нет поля «отчество», сотрудники писали фамилию + имя в last_name,
отчество — в first_name) и распарсивает через DaData Clean API,
возвращая корректные фамилию / имя / отчество.

Вызывается из sync-view: identify_get_telegram_info(telegram_id) →
{"ok": bool, "first_name", "last_name", "username", "phone",
 "phone_via_contact",
 "parsed_surname", "parsed_name", "parsed_patronymic", "parsed_qc",
 "parsed_error", "error"}.
"""
import asyncio
import logging
import os

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import AddContactRequest, DeleteContactsRequest
from telethon.tl.types import InputUser, InputPeerUser, PeerUser, User as TLUser
from django.conf import settings

logger = logging.getLogger("userbot")


DADATA_CLEAN_URL = "https://cleaner.dadata.ru/api/v1/clean/name"


def _parse_fio_via_dadata(raw: str) -> dict:
    """
    Отправляет строку в DaData Clean API и возвращает распарсенные
    surname/name/patronymic + qc (0=уверенно, 1=частично, 2=нет).
    """
    api_key = getattr(settings, "DADATA_API_KEY", "")
    secret_key = getattr(settings, "DADATA_SECRET_KEY", "")
    if not api_key or not secret_key:
        return {"ok": False, "error": "DADATA_API_KEY/SECRET_KEY не заданы"}
    try:
        resp = requests.post(
            DADATA_CLEAN_URL,
            json=[raw],
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {api_key}",
                "X-Secret": secret_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        item = resp.json()[0]
        return {
            "ok": True,
            "surname": (item.get("surname") or "").strip(),
            "name": (item.get("name") or "").strip(),
            "patronymic": (item.get("patronymic") or "").strip(),
            "qc": item.get("qc", 2),
        }
    except Exception as e:
        logger.warning(f"DaData parse failed for {raw!r}: {e}")
        return {"ok": False, "error": str(e)}


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
        "parsed_surname": "",
        "parsed_name": "",
        "parsed_patronymic": "",
        "parsed_qc": None,
        "parsed_error": "",
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
    """Sync-обёртка. Запускает новый event loop (как telegram_sender)
    и затем парсит склеенное ФИО через DaData Clean API."""
    if not telegram_id:
        return {"ok": False, "error": "У клиента не задан telegram_id."}
    try:
        result = asyncio.run(_async_identify(int(telegram_id), try_add_contact=try_add_contact))
    except Exception as e:
        logger.exception(f"identify_get_telegram_info({telegram_id}) failed: {e}")
        return {"ok": False, "error": str(e)}

    if not result.get("ok"):
        return result

    # В Telegram нет поля «отчество» — сотрудники писали:
    #   last_name = "Фамилия Имя", first_name = "Отчество".
    # Склеиваем в одну строку и доверяем DaData разложить на 3 части.
    raw_fio = " ".join(
        p for p in [
            (result.get("last_name") or "").strip(),
            (result.get("first_name") or "").strip(),
        ] if p
    ).strip()

    if not raw_fio:
        return result

    parsed = _parse_fio_via_dadata(raw_fio)
    if parsed.get("ok"):
        result["parsed_surname"]    = parsed.get("surname", "")
        result["parsed_name"]       = parsed.get("name", "")
        result["parsed_patronymic"] = parsed.get("patronymic", "")
        result["parsed_qc"]         = parsed.get("qc")
    else:
        result["parsed_error"] = parsed.get("error", "")
    return result
