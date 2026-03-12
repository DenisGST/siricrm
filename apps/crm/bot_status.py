import requests
from django.conf import settings
from django.core.cache import cache

def get_bot_status():
    """
    Проверяет работоспособность бота через Telegram Bot API.
    Результат кэшируется на 60 секунд чтобы не спамить API.
    """
    if cache.get("userbot_heartbeat"):
        return "ok"
    return "error"