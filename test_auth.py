import asyncio
from telethon import TelegramClient

API_ID = 5097446
API_HASH = '26a4c92512e469d86e824c89e10ce8a4'
PHONE = '+79610730606'

async def test():
    client = TelegramClient('test_full_session', API_ID, API_HASH)
    
    print(f"📱 Starting auth for {PHONE}...")
    await client.start(phone=PHONE)
    
    print("✅ Successfully authorized!")
    me = await client.get_me()
    print(f"Logged in as: {me.first_name} {me.last_name} (@{me.username})")
    
    await client.disconnect()

asyncio.run(test())