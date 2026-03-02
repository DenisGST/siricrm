import asyncio
from telethon import TelegramClient
from telethon.tl.functions.auth import SendCodeRequest, ResendCodeRequest
from telethon.tl.types import CodeSettings

API_ID = 5097446
API_HASH = '26a4c92512e469d86e824c89e10ce8a4'
PHONE = '+79610730606'

async def main():
    client = TelegramClient('userbot_session', API_ID, API_HASH)
    await client.connect()

    # Первый запрос (в приложение)
    result = await client(SendCodeRequest(
        phone_number=PHONE,
        api_id=API_ID,
        api_hash=API_HASH,
        settings=CodeSettings()
    ))
    print(f"First code type: {result.type.__class__.__name__}")

    # Запрашиваем повторно — Telegram должен переключиться на SMS
    result2 = await client(ResendCodeRequest(
        phone_number=PHONE,
        phone_code_hash=result.phone_code_hash
    ))
    print(f"Resend code type: {result2.type.__class__.__name__}")
    # Теперь должно быть SentCodeTypeSms

    code = input("Enter SMS code: ")
    await client.sign_in(
        phone=PHONE,
        code=code,
        phone_code_hash=result2.phone_code_hash
    )

    me = await client.get_me()
    print(f"✅ Logged in as: {me.first_name} (@{me.username})")
    await client.disconnect()

asyncio.run(main())