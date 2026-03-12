import requests
from django.conf import settings
from django.core.cache import cache

def get_bot_status():
    """
    Проверяет работоспособность бота через Telegram Bot API.
    Результат кэшируется на 60 секунд чтобы не спамить API.
    """
    cached = cache.get("bot_status")
    if cached:
        return cached

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        cache.set("bot_status", "error", 60)
        return "error"

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=5,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            status = "ok"
        else:
            status = "error"
    except Exception:
        status = "error"

    cache.set("bot_status", status, 60)
    return status