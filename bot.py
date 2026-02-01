import os
import django
import logging

from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings
from django.utils import timezone
from apps.auth_telegram.models import TelegramUser, TelegramAuthCode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "Привет! Отправь мне команду /auth КОД, который видишь в CRM, чтобы привязать Telegram."
  )


async def auth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  try:
      tg_user = update.effective_user
      message = update.message

      logger.warning("AUTH_COMMAND CALLED from %s", tg_user.id)

      parts = message.text.split(maxsplit=1)
      if len(parts) != 2:
          await message.reply_text("Используйте: /auth КОД, показанный на сайте.")
          return

      code = parts[1].strip().upper()

      try:
          code_obj = TelegramAuthCode.objects.get(code=code, is_used=False)
      except TelegramAuthCode.DoesNotExist:
          await message.reply_text("❌ Неверный или просроченный код.")
          return

      if code_obj.is_expired():
          await message.reply_text("⏰ Код истёк. Сгенерируйте новый на сайте.")
          return

      django_user = code_obj.user

      TelegramUser.objects.update_or_create(
          telegram_id=tg_user.id,
          defaults={
              "user": django_user,
              "first_name": tg_user.first_name or "",
              "last_name": tg_user.last_name or "",
              "username": tg_user.username or "",
              "is_verified": True,
              "last_login": timezone.now(),
          },
      )

      code_obj.is_used = True
      code_obj.save()

      await message.reply_text(
          "✅ Ваш Telegram успешно привязан к аккаунту CRM.\n"
          "Теперь вы можете работать через бота."
      )
  except Exception as e:
      logger.exception("Error in /auth: %s", e)
      await update.message.reply_text(
          "⚠️ Внутренняя ошибка при привязке. Сообщите админу."
      )


async def main():
  application = (
      Application.builder()
      .token(settings.TELEGRAM_BOT_TOKEN)  # см. ниже про настройки
      .build()
  )

  application.add_handler(CommandHandler("start", start))
  application.add_handler(CommandHandler("auth", auth_command))

  logger.info("Starting Telegram bot polling...")
  await application.initialize()
  await application.start()
  await application.updater.start_polling()
  await application.updater.wait_for_stop()
  await application.stop()
  await application.shutdown()


if __name__ == "__main__":
  import asyncio
  asyncio.run(main())
