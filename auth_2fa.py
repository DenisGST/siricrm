import asyncio
from telethon import TelegramClient
from telethon.tl.functions.auth import SendCodeRequest
from telethon.tl.types import CodeSettings

API_ID = 5097446
API_HASH = '26a4c92512e469d86e824c89e10ce8a4'
PHONE = '+79610730606'

async def main():
    client = TelegramClient('userbot_session', API_ID, API_HASH)
    await client.connect()

    result = await client(SendCodeRequest(
        phone_number=PHONE,
        api_id=API_ID,
        api_hash=API_HASH,
        settings=CodeSettings()
    ))

    print(f"Code type: {result.type.__class__.__name__}")
    print("Открой Telegram Web — код придёт туда!")
    print("Telegram Web: https://web.telegram.org")
    
    code = input("Введи код: ")
    
    try:
        await client.sign_in(
            phone=PHONE,
            code=code,
            phone_code_hash=result.phone_code_hash
        )
    except Exception as e:
        if "PASSWORD" in str(type(e).__name__).upper() or "2FA" in str(e).upper():
            password = input("Введи пароль 2FA: ")
            await client.sign_in(password=password)
        else:
            raise

    me = await client.get_me()
    print(f"✅ Вошёл как: {me.first_name} (@{me.username})")
    await client.disconnect()

asyncio.run(main())
