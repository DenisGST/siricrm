from django.conf import settings
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from .handlers import TelegramHandlers


application = (
    Application.builder()
    .token(settings.TELEGRAM_BOT_TOKEN)
    .build()
)

# Регистрируем все хэндлеры ОДИН РАЗ
application.add_handler(CommandHandler("start", TelegramHandlers.start_command))
application.add_handler(CommandHandler("help", TelegramHandlers.help_command))
application.add_handler(CommandHandler("status", TelegramHandlers.status_command))
application.add_handler(CommandHandler("auth", TelegramHandlers.auth_command))

application.add_handler(
    MessageHandler(filters.PHOTO, TelegramHandlers.handle_photo)
)
application.add_handler(
    MessageHandler(filters.Document.ALL, TelegramHandlers.handle_document)
)
application.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, TelegramHandlers.handle_message)
)

