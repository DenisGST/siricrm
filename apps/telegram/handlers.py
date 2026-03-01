# /apps/telegram/handlers.py
import logging
import uuid
import re
import os
import tempfile

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from django.contrib.auth.models import User
from django.utils import timezone
from django.conf import settings
from asgiref.sync import sync_to_async

from apps.crm.models import Client, Message
from apps.core.models import Employee, EmployeeLog
from apps.auth_telegram.models import TelegramUser, TelegramAuthCode
from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3
from apps.realtime.utils import push_chat_message, push_client_toast


# from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.template.loader import render_to_string


#from .s3_utils import upload_telegram_file_to_s3

logger = logging.getLogger(__name__)

WORK_START_HOUR = 9
WORK_END_HOUR = 18
MSK_TZ = timezone.get_fixed_timezone(3 * 60)  # UTC+3
PHONE_RE = re.compile(r"[+\d][\d\-\s\(\)]{5,}")  # очень мягко, под российские номера

async def bot_reply_and_log(
    *,
    client: Client,
    chat_id: int,
    text: str,
    context: ContextTypes.DEFAULT_TYPE,
    employee: Employee | None = None,
) -> None:
    """
    Отправляет сообщение от бота и сохраняет его в CRM.Message как исходящее.
    """
    sent_msg = await context.bot.send_message(chat_id=chat_id, text=text)

    msg = await sync_to_async(Message.objects.create)(
        client=client,
        employee=employee,
        content=text,
        message_type="text",
        direction="outgoing",
        telegram_message_id=sent_msg.message_id,
    )
    await sync_to_async(push_chat_message)(msg)
# парсер моб.тел, ФИО 
def parse_phone_and_fio(text: str):
    """
    Ожидаем что-то вроде:
    8 999 123-45-67 Иванов Иван Иванович
    +7 (999) 123-45-67 Петров Петр
    Возвращаем: phone, last_name, first_name, patronymic
    """
    text = text.strip()
    m = PHONE_RE.search(text)
    if not m:
        return None, None, None, None

    phone_raw = m.group(0)
    # нормализуем телефон: оставим только цифры и '+'
    phone = re.sub(r"[^\d+]", "", phone_raw)

    # остальная часть — ФИО
    fio_part = (text[:m.start()] + " " + text[m.end():]).strip()
    fio_tokens = [t for t in fio_part.split() if t]

    last_name = first_name = patronymic = ""

    if len(fio_tokens) == 1:
        last_name = fio_tokens[0]
    elif len(fio_tokens) == 2:
        last_name, first_name = fio_tokens
    elif len(fio_tokens) >= 3:
        last_name, first_name, patronymic = fio_tokens[0], fio_tokens[1], " ".join(fio_tokens[2:])

    return phone, first_name, last_name, patronymic

# Хелпер: рабочее время по МСК
def is_working_time(dt: timezone.datetime) -> bool:
    dt_msk = dt.astimezone(MSK_TZ)
    if dt_msk.weekday() >= 5:  # 5,6 = сб, вс
        return False
    if WORK_START_HOUR <= dt_msk.hour < WORK_END_HOUR:
        return True
    return False

class TelegramHandlers:
    """Handlers for Telegram bot interactions"""

    # ---------- AUTH ----------

    @staticmethod
    async def auth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /auth CODE — привязка Telegram к уже существующему Django-пользователю.
        """
        tg_user = update.effective_user
        message = update.message

        parts = message.text.split(maxsplit=1)
        if len(parts) != 2:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Используйте: /auth КОД, показанный на сайте.",
            )
            return

        code = parts[1].strip().upper()

        try:
            code_obj = await sync_to_async(TelegramAuthCode.objects.get)(
                code=code,
                is_used=False,
            )
        except TelegramAuthCode.DoesNotExist:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Неверный или просроченный код.",
            )
            return

        if code_obj.is_expired():
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⏰ Код истёк. Сгенерируйте новый на сайте.",
            )
            return

        django_user = await sync_to_async(lambda: code_obj.user)()

        await sync_to_async(TelegramUser.objects.update_or_create)(
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
        await sync_to_async(code_obj.save)()

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "✅ Ваш Telegram успешно привязан к аккаунту CRM.\n"
                "Теперь вы можете работать через бота."
            ),
        )

    # ---------- BASIC COMMANDS ----------

    @staticmethod
    async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user

        try:
            telegram_user = await sync_to_async(TelegramUser.objects.get)(
                telegram_id=user.id
            )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"Привет, {user.first_name}! 👋\n\n"
                    f"Вы уже зарегистрированы в системе."
                ),
            )
        except TelegramUser.DoesNotExist:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "Зарегистрироваться в CRM",
                        url=f"https://yourdomain.com/telegram/login/{user.id}/",
                    )
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"Привет, {user.first_name}! 👋\n\n"
                    f"Добро пожаловать в CRM систему!\n"
                    f"Нажмите кнопку ниже для регистрации."
                ),
                reply_markup=reply_markup,
            )

    @staticmethod
    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """
                    🤖 Доступные команды:
                    /start - Начало работы
                    /help - Эта справка
                    💬 Отправляйте сообщения в СИРИУС прямо через бота!
        """
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=help_text,
        )

    
    # ---------- ROUTING MESSAGES ----------
    @staticmethod
    async def _auto_reply_first_message(context: ContextTypes.DEFAULT_TYPE):
        chat_id = context.job.data["chat_id"]
        client_id = context.job.data.get("client_id")

        text = (
            "Добрый день, мы приняли ваше сообщение. "
            "Вам в ближайшее время ответит наш специалист."
        )

        sent_msg = await context.bot.send_message(chat_id=chat_id, text=text)

        if client_id:
            client = await sync_to_async(Client.objects.get)(pk=client_id)
            msg = await sync_to_async(Message.objects.create)(
                client=client,
                employee=None,
                content=text,
                message_type="text",
                direction="outgoing",
                telegram_message_id=sent_msg.message_id,
            )
            await sync_to_async(push_chat_message)(msg)
            await sync_to_async(push_client_toast)(
                client,text=f"Новое сообщение от клиента {client.first_name or client.id}",
            )

    @staticmethod
    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming text messages from clients or employees"""
        user = update.effective_user
        message = update.message

        if not user or not message or not message.text:
            return

        now = timezone.now()

        try:
            
            client = await sync_to_async(
                Client.objects.filter(telegram_id=user.id).first
            )()

            # --- a) Клиент впервые пишет (в Client его ещё нет) ---
            if client is None:
                client = await sync_to_async(Client.objects.create)(
                    telegram_id=user.id,
                    first_name=user.first_name or "",
                    last_name=user.last_name or "",
                    username=user.username or "",
                    status="lead",
                    last_message_at=now,
                )
                logger.info("Auto-registered new client: %s", client)

                # сохраняем это первое сообщение как обычное
                msg = await sync_to_async(Message.objects.create)(
                    client=client,
                    employee=None,
                    content=message.text,
                    message_type="text",
                    direction="incoming",
                    telegram_message_id=message.message_id,
                )
                await sync_to_async(push_chat_message)(msg)
                await sync_to_async(push_client_toast)(
                    client,text=f"88Новое сообщение от клиента {client.first_name or client.id}",
                )


                await bot_reply_and_log(
                    client=client,
                    chat_id=message.chat_id,
                    text=(
                        "Здравствуйте! Похоже, вы обращаетесь к нам впервые.\n\n"
                        "Пожалуйста, отправьте ваш номер телефона и фамилию, имя, отчество одной строкой.\n\n"
                        "Пример: 8 999 123-45-67 Иванов Иван Иванович"
                    ),
                    context=context,
                )
                return

            # Клиент уже есть — обновляем last_message_at
            await sync_to_async(Client.objects.filter(pk=client.pk).update)(
                last_message_at=now
            )

             # --- если у клиента ещё нет телефона, пробуем разобрать текущее сообщение как телефон+ФИО ---
            if not client.phone:
                phone, first_name, last_name, patronymic = parse_phone_and_fio(message.text)
                if phone:
                    client.phone = phone
                    if first_name:
                        client.first_name = first_name
                    if last_name:
                        client.last_name = last_name
                    if patronymic:
                        client.patronymic = patronymic
                    client.contacts_confirmed = True 
                    await sync_to_async(client.save)()

                    await bot_reply_and_log(
                        client=client,
                        chat_id=message.chat_id,
                        text="Спасибо! Мы сохранили ваши контактные данные и готовы выслушать ваше обращение.",
                        context=context,
                    )
                    # дальше продолжаем обычную логику (сохранение самого сообщения и т.п.)
                    # но чтобы не дублировать, можно просто не выходить здесь
                else:
                    # не смогли распарсить телефон — вежливо попросим в нужном формате
                    await message.reply_text(
                        "Не удалось распознать номер телефона.\n\n"
                        "Пожалуйста, отправьте фамилию, имя, отчество и номер телефона "
                        "одной строкой.\n\n"
                        "Пример: 8 999 123-45-67 Иванов Иван Иванович"
                    )
                    # можно завершить обработку, чтобы это сообщение не шло дальше как обычное
                    return

            """
            # --- b) Клиент есть, проверяем рабочее/нерабочее время ---
            if not is_working_time(now):
                await bot_reply_and_log(
                    client=client,
                    chat_id=message.chat_id,
                    text=(
                        "Мы приняли Ваше сообщение, однако мы работаем с 9:00 до 18:00 по МСК, "
                        "кроме выходных и праздничных дней. Поэтому Вам ответит наш специалист "
                        "в начале рабочего дня. Надеемся на понимание."
                    ),
                    context=context,
                )
            """
            # сохраняем сообщение в CRM
            msg = await sync_to_async(Message.objects.create)(
                client=client,
                content=message.text,
                message_type="text",
                direction="incoming",
                telegram_message_id=message.message_id,
            )
            await sync_to_async(push_chat_message)(msg)
            await sync_to_async(push_client_toast)(
                client,text=f"2Новое сообщение от клиента {client.first_name or client.id}",
            )
            return

            # --- c) Клиент есть, сейчас рабочее время ---
            # Сохраняем сообщение в CRM
            msg = await sync_to_async(Message.objects.create)(
                client=client,
                content=message.text,
                message_type="text",
                direction="incoming",
                telegram_message_id=message.message_id,
            )
            await sync_to_async(push_chat_message)(msg)
            await sync_to_async(push_client_toast)(
                client,text=f"3Новое сообщение от клиента {client.first_name or client.id}",
            )

            # Логика «если клиент к нам обращался ранее и ничего не писал после первого обращения»
            # Интерпретация: это его самое первое сообщение (нет других incoming-сообщений до этого)
            previous_msgs_exist = await sync_to_async(
                lambda: Message.objects.filter(
                    client=client,
                    direction="incoming",
                )
                .exclude(telegram_message_id=message.message_id)
                .exists()
            )()


        except Exception as e:
            logger.exception("Error handling message: %s", e)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Ошибка при обработке сообщения.",
            )


    
    # ---------- FILES: PHOTO & DOCUMENT ----------

    @staticmethod
    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        message = update.message

        photo = message.photo[-1]
        tg_file = await photo.get_file()
        file_bytes = await tg_file.download_as_bytearray()

        filename = f"photo_{photo.file_unique_id}.jpg"

        # 1) грузим в S3 через новый helper -> получаем bucket, key
        bucket, key = upload_file_to_s3(
            bytes(file_bytes),
            prefix="telegram/images",
            filename=filename,
        )

        # 2) создаём StoredFile
        stored_file = await sync_to_async(StoredFile.objects.create)(
            bucket=bucket,
            key=key,
            filename=filename,
        )

        # 3) находим/создаём клиента
        client = await TelegramHandlers._get_or_create_client_for_user(user)

        # 4) создаём Message с FK на StoredFile
        msg = await sync_to_async(Message.objects.create)(
            employee=None,
            client=client,
            message_type="image",
            content="Изображение от клиента",
            telegram_message_id=message.message_id,
            file=stored_file,
            direction="incoming",
        )
        await sync_to_async(push_chat_message)(msg)
        await sync_to_async(push_client_toast)(
                client,text=f"Фото от клиента {client.first_name or client.id}",
            )

        # 5) ответ клиенту
        await context.bot.send_message(
            chat_id=message.chat_id,
            text="📷 Картинка получена и сохранена.",
        )

    @staticmethod
    async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user = update.effective_user
            message = update.message
            doc = message.document

            if doc is None:
                logger.warning("handle_document called but message.document is None: %s", message.to_dict())
                return

            tg_file = await doc.get_file()
            file_bytes = await tg_file.download_as_bytearray()

            filename = doc.file_name or f"file_{doc.file_unique_id}"

            # 1) грузим в S3
            bucket, key = upload_file_to_s3(
                bytes(file_bytes),
                prefix="telegram/docs",
                filename=filename,
            )

            # 2) StoredFile
            stored_file = await sync_to_async(StoredFile.objects.create)(
                bucket=bucket,
                key=key,
                filename=filename,
            )

            # 3) клиент
            client = await TelegramHandlers._get_or_create_client_for_user(user)

            # 4) Message с FK на StoredFile
            msg = await sync_to_async(Message.objects.create)(
                employee=None,
                client=client,
                message_type="document",
                content=f"Файл: {filename}",
                telegram_message_id=message.message_id,
                file=stored_file,
                direction="incoming",
            )
            await sync_to_async(push_chat_message)(msg)
            await sync_to_async(push_client_toast)(
                client,text=f"Файл от клиента {client.first_name or client.id}",
            )

            # 5) ответ
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"📎 Файл «{filename}» получен и сохранён.",
            )

        except Exception as e:
            logger.exception("Error in handle_document: %s", e)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Ошибка при обработке файла.",
            )

    # ---------- UTILS ----------

    @staticmethod
    async def _get_or_create_client_for_user(user):
        client = await sync_to_async(
            Client.objects.filter(telegram_id=user.id).first
        )()
        if client:
            return client

        client = await sync_to_async(Client.objects.create)(
            telegram_id=user.id,
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            username=user.username or "",
        )
        return client

    @staticmethod
    async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Фото / голосовые / аудио / видео / документы в одном хендлере.
        """
        try:
            user = update.effective_user
            message = update.message
            if not user or not message:
                return

            tg_obj = None
            message_type = "document"
            filename = ""

            if message.voice:
                tg_obj = message.voice
                message_type = "audio"
                filename = f"voice_{tg_obj.file_unique_id}.ogg"
            elif message.audio:
                tg_obj = message.audio
                message_type = "audio"
                filename = tg_obj.file_name or f"audio_{tg_obj.file_unique_id}.ogg"
            elif message.video:
                tg_obj = message.video
                message_type = "video"
                filename = tg_obj.file_name or f"video_{tg_obj.file_unique_id}.mp4"
            elif message.photo:
                tg_obj = message.photo[-1]
                message_type = "image"
                filename = f"photo_{tg_obj.file_unique_id}.jpg"
            elif message.document:
                tg_obj = message.document
                message_type = "document"
                filename = tg_obj.file_name or f"file_{tg_obj.file_unique_id}"

            if tg_obj is None:
                logger.warning("handle_media called but no media in message: %s", message.to_dict())
                return

            # 1) грузим в S3 (аналогично handle_document)
            tg_file = await tg_obj.get_file()
            file_bytes = await tg_file.download_as_bytearray()

            prefix_map = {
                "audio": "telegram/audio",
                "video": "telegram/video",
                "image": "telegram/photos",
                "document": "telegram/docs",
            }
            prefix = prefix_map.get(message_type, "telegram/docs")

            bucket, key = upload_file_to_s3(
                bytes(file_bytes),
                prefix=prefix,
                filename=filename,
            )

            # 2) StoredFile
            stored_file = await sync_to_async(StoredFile.objects.create)(
                bucket=bucket,
                key=key,
                filename=filename,
            )

            # 3) клиент
            client = await TelegramHandlers._get_or_create_client_for_user(user)

            # 4) Message
            msg = await sync_to_async(Message.objects.create)(
                employee=None,
                client=client,
                message_type=message_type,
                content=f"Файл: {filename}" if message_type != "audio" else "",
                telegram_message_id=message.message_id,
                file=stored_file,
                direction="incoming",
            )
            await sync_to_async(push_chat_message)(msg)
            await sync_to_async(push_client_toast)(
                client,
                text=f"Медиа от клиента {client.first_name or client.id}",
            )

            # 5) ответ пользователю
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"🎧 Голосовое получено и сохранено." if message_type == "audio"
                    else f"🎥 Видео получено и сохранено." if message_type == "video"
                    else f"🖼 Фото получено и сохранено." if message_type == "image"
                    else f"📎 Файл «{filename}» получен и сохранён.",
            )

        except Exception as e:
            logger.exception("Error in handle_media: %s", e)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Ошибка при обработке медиа.",
            )

###############################################################################
# ---- SEND MASSAGE FRO CHAT

async def send_text_from_crm(
    *,
    client: Client,
    text: str,
    employee: Employee | None = None,
) -> None:
    """
    Асинхронно отправляет сообщение клиенту из CRM
    и сохраняет его в Message как исходящее.
    Используется и из бота, и из Django-части.
    """
    if application is None:
        # если бот не инициализирован — просто логируем/падаем тихо
        logger.error("Telegram application is not initialized")
        return

    chat_id = client.telegram_id
    if not chat_id:
        logger.error("Client %s has no telegram_id", client)
        return

    # отправляем через уже существующий application.bot
    sent_msg = await application.bot.send_message(chat_id=chat_id, text=text)

    # пишем в CRM
    msg = await sync_to_async(Message.objects.create)(
        client=client,
        employee=employee,
        content=text,
        message_type="text",
        direction="outgoing",
        telegram_message_id=sent_msg.message_id,
    )
    await sync_to_async(push_chat_message)(msg)
    await sync_to_async(push_client_toast)(
                client,text=f"Отправлено клиенту {client.first_name or client.id}",
            )


