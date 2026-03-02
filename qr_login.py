import os
import asyncio
from telethon import TelegramClient, errors
from qrcode import QRCode

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PASSWORD = os.getenv("TELEGRAM_2FA_PASSWORD")  # если включена 2FA-пароль Telegram

async def main():
    client = TelegramClient("userbot_session", API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        print("Already authorized")
        await client.disconnect()
        return

    qr_login = await client.qr_login()

    qr = QRCode()
    qr.add_data(qr_login.url)
    qr.print_ascii(tty=True)

    try:
        await qr_login.wait(timeout=180)
    except errors.SessionPasswordNeededError:
        if not PASSWORD:
            print("2FA включена: добавь TELEGRAM_2FA_PASSWORD в окружение/.env и повтори")
            await client.disconnect()
            return
        await client.sign_in(password=PASSWORD)

    print("Authorized OK")
    await client.disconnect()

asyncio.run(main())
