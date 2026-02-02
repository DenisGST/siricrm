# bot_status.py
from datetime import timedelta
from django.utils import timezone

def get_bot_status():
    """
    Простая заглушка статуса бота.
    Потом сюда добавим реальные проверки.
    """
    # TODO: здесь будет реальная логика
    # Например:
    # - проверить запись в БД
    # - проверить очередь задач
    # - попробовать сделать ping Telegram API

    # Пока всегда "ok":
    return "ok"
