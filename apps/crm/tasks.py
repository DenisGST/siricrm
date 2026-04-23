# apps/crm/tasks.py

from datetime import timedelta
import logging
import asyncio

from celery import shared_task
from django.conf import settings
from django.utils import timezone
from django.db.models import Count
from apps.crm.models import Client, Message
from apps.core.models import Employee, EmployeeLog, Department
from apps.files.s3_utils import download_file_from_s3
from apps.telegram.telegram_sender import send_telegram_message
from apps.maxchat.sender import send_max_message

logger = logging.getLogger('celery')


@shared_task
def cleanup_old_logs(days=30):
    cutoff_date = timezone.now() - timedelta(days=days)
    deleted_count, _ = EmployeeLog.objects.filter(timestamp__lt=cutoff_date).delete()
    logger.info(f"Deleted {deleted_count} old logs")
    return f"Deleted {deleted_count} logs older than {days} days"


@shared_task
def generate_daily_report():
    today = timezone.now().date()
    report_data = {}
    try:
        for dept in Department.objects.filter(is_active=True):
            employees = dept.employees.filter(is_active=True)
            report_data[dept.name] = {
                "employees_count": employees.count(),
                "messages_sent": 0,
                "messages_received": 0,
                "new_clients": 0,
                "active_clients": 0,
                "employee_stats": [],
            }

            for employee in employees:
                messages_sent = Message.objects.filter(
                    employee=employee,
                    direction="outgoing",
                    created_at__date=today,
                ).count()

                messages_received = Message.objects.filter(
                    client__employees=employee,
                    direction="incoming",
                    created_at__date=today,
                ).count()

                actions = EmployeeLog.objects.filter(
                    employee=employee,
                    timestamp__date=today,
                )

                report_data[dept.name]["messages_sent"] += messages_sent
                report_data[dept.name]["messages_received"] += messages_received
                report_data[dept.name]["employee_stats"].append(
                    {
                        "employee": employee.user.get_full_name(),
                        "messages_sent": messages_sent,
                        "messages_received": messages_received,
                        "actions_count": actions.count(),
                        "clients_count": Client.objects.filter(employees=employee).count(),
                    }
                )

            new_clients = Client.objects.filter(
                employees__in=employees,
                created_at__date=today,
            ).distinct().count()
            report_data[dept.name]["new_clients"] = new_clients

            active_clients = Client.objects.filter(
                employees__in=employees,
                status="active",
            ).distinct().count()
            report_data[dept.name]["active_clients"] = active_clients

        logger.info(f"Generated daily report: {report_data}")
        return report_data
    except Exception as e:
        logger.error(f"Error generating daily report: {e}")
        return {"error": str(e)}


@shared_task
def sync_employee_status():
    timeout_minutes = 5
    cutoff_time = timezone.now() - timedelta(minutes=timeout_minutes)
    try:
        online_employees = Employee.objects.filter(is_online=True)
        for employee in online_employees:
            last_action = (
                EmployeeLog.objects.filter(employee=employee)
                .order_by("-timestamp")
                .first()
            )
            if last_action and last_action.timestamp < cutoff_time:
                employee.is_online = False
                employee.save(update_fields=["is_online"])
                logger.info(f"Marked {employee} as offline due to inactivity")
        return f"Updated status for {online_employees.count()} employees"
    except Exception as e:
        logger.error(f"Error syncing employee status: {e}")
        return {"error": str(e)}


@shared_task
def send_telegram_message_task(message_id):
    """
    Отправка сообщения через Telegram userbot с поддержкой медиа
    """
    from apps.crm.models import Message
    from django.utils import timezone
    import asyncio
    
    logger.info(f"📤 Starting task: send_telegram_message_task for message_id={message_id}")

    # Создаём новый event loop для этой задачи
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        message = Message.objects.select_related('client', 'file', 'reply_to').get(id=message_id)
        
        # Подготовка параметров для отправки
        send_params = {
            'telegram_id': message.client.telegram_id,
            'text': message.content,
            'message_type': message.message_type,
        }
        
        # Если есть файл, добавляем его
        if message.file:
            from apps.files.s3_utils import download_file_from_s3
            
            # Скачиваем файл из S3
            file_bytes = download_file_from_s3(message.file.bucket, message.file.key)
            
            send_params['file_bytes'] = file_bytes
            send_params['file_name'] = message.file.filename
            
            logger.info(f"📦 Downloaded file {message.file.filename} from S3 for message {message_id}")
        
        if message.reply_to_id and message.reply_to and message.reply_to.telegram_message_id:
            send_params['reply_to_msg_id'] = int(message.reply_to.telegram_message_id)
        
        # Отправляем через userbot в новом event loop
        result = loop.run_until_complete(send_telegram_message(**send_params))
        
        if result['success']:
            message.is_sent = True
            message.telegram_message_id = result['message_id']
            message.sent_at = timezone.now()
            message.telegram_date = timezone.now()
            message.save(update_fields=['is_sent', 'telegram_message_id', 'sent_at', 'telegram_date'])
            logger.info(f"✅ Task: Message {message_id} sent successfully")

            try:
                from apps.realtime.utils import push_message_status, push_toast
                push_message_status(message)
                if message.employee and message.employee.user:
                    push_toast(message.employee.user, "Сообщение отправлено", level="success")
            except Exception as e:
                logger.warning(f"Failed to push WS update: {e}")
        else:
            logger.error(f"❌ Task: Failed to send message {message_id}: {result['error']}")
            try:
                from apps.realtime.utils import push_toast
                if message.employee and message.employee.user:
                    push_toast(message.employee.user, f"Ошибка отправки: {result['error']}", level="error")
            except Exception as e:
                logger.warning(f"Failed to push toast: {e}")

            
    except Message.DoesNotExist:
        logger.error(f"❌ Task: Message {message_id} not found")
    except Exception as e:
        logger.exception(f"❌ Task error for message {message_id}: {e}")
    finally:
        # Закрываем event loop
        loop.close()

@shared_task
def reassign_clients_by_load():
    try:
        # Клиенты без сотрудников
        unassigned_clients = Client.objects.filter(
            employees__isnull=True,
            status__in=["lead", "active"],
        )
        
        for client in unassigned_clients:
            # Выбираем отдел с наименьшей нагрузкой
            dept = Department.objects.annotate(
                employee_count=Count("employees"),
                total_clients=Count("employees__client"),
            ).order_by("total_clients").first()
            
            if not dept:
                continue
                
            # Выбираем сотрудника с наименьшей нагрузкой
            employee = (
                Employee.objects.filter(department=dept, is_active=True)
                .annotate(client_count=Count("client"))
                .order_by("client_count")
                .first()
            )
            
            if employee:
                from apps.crm.models import ClientEmployee
                ClientEmployee.objects.get_or_create(client=client, employee=employee)
                EmployeeLog.objects.create(
                    employee=employee,
                    action="client_assigned",
                    description=f"Клиент {client} назначен системой",
                    client=client,
                )
                
        logger.info(f"Reassigned {unassigned_clients.count()} clients")
        return f"Reassigned {unassigned_clients.count()} clients"
    except Exception as e:
        logger.error(f"Error reassigning clients: {e}")
        return {"error": str(e)}

@shared_task
def generate_employee_stats(employee_id, start_date=None, end_date=None):
    try:
        employee = Employee.objects.get(id=employee_id)
        if not start_date:
            start_date = timezone.now() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now()

        stats = {
            "employee": employee.user.get_full_name(),
            "period": f"{start_date.date()} to {end_date.date()}",
            "total_messages": Message.objects.filter(
                employee=employee,
                created_at__range=[start_date, end_date],
            ).count(),
            "clients_assigned": Client.objects.filter(employees=employee).count(),
            "messages_by_type": {},
            "daily_activity": {},
        }

        messages_by_type = (
            Message.objects.filter(
                employee=employee,
                created_at__range=[start_date, end_date],
            )
            .values("message_type")
            .annotate(count=Count("id"))
        )
        for item in messages_by_type:
            stats["messages_by_type"][item["message_type"]] = item["count"]

        daily_data = (
            EmployeeLog.objects.filter(
                employee=employee,
                timestamp__range=[start_date, end_date],
            )
            .values("timestamp__date")
            .annotate(count=Count("id"))
        )
        for item in daily_data:
            stats["daily_activity"][str(item["timestamp__date"])] = item["count"]

        logger.info(f"Generated stats for {employee}: {stats}")
        return stats
    except Employee.DoesNotExist:
        logger.error(f"Employee {employee_id} not found")
        return {"error": "Employee not found"}
    except Exception as e:
        logger.error(f"Error generating employee stats: {e}")
        return {"error": str(e)}


@shared_task
def archive_old_messages(days=90):
    try:
        cutoff_date = timezone.now() - timedelta(days=days)
        old_messages = Message.objects.filter(created_at__lt=cutoff_date)
        archived_count = old_messages.count()
        logger.info(f"Archived {archived_count} messages older than {days} days")
        return f"Archived {archived_count} messages"
    except Exception as e:
        logger.error(f"Error archiving messages: {e}")
        return {"error": str(e)}

@shared_task
def import_telegram_history_task(telegram_id, limit=300):
    import asyncio
    import os
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import PeerUser, MessageMediaPhoto, MessageMediaDocument
    from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeFilename
    from asgiref.sync import sync_to_async

    api_id = int(os.environ['TELEGRAM_API_ID'])
    api_hash = os.environ['TELEGRAM_API_HASH']
    session_string = os.environ['TELEGRAM_SESSION_STRING']

    async def do_import():
        from apps.crm.models import Client, Message
        from apps.files.s3_utils import upload_file_to_s3
        from apps.files.models import StoredFile

        db_client = await sync_to_async(
            Client.objects.filter(telegram_id=telegram_id).first
        )()
        if not db_client:
            logger.warning(f"Client {telegram_id} not found")
            return

        async with TelegramClient(StringSession(session_string), api_id, api_hash) as tg:
            peer = await tg.get_entity(PeerUser(telegram_id))
            history = await tg.get_messages(peer, limit=limit)

            imported_count = 0
            for msg in reversed(history):
                # пропускаем только полностью пустые (нет текста И нет медиа)
                if not msg.message and not msg.media:
                    continue

                exists = await sync_to_async(
                    Message.objects.filter(telegram_message_id=msg.id).exists
                )()
                if exists:
                    continue

                direction = "outgoing" if msg.out else "incoming"
                content = msg.message or ""
                message_type = "text"
                file_data = None
                file_name = ""

                # ── Обработка медиа ─────────────────────────────────────
                if msg.media:
                    if isinstance(msg.media, MessageMediaDocument):
                        doc = msg.media.document
                        is_voice = False
                        is_audio = False
                        original_filename = "file"

                        for attr in doc.attributes:
                            if isinstance(attr, DocumentAttributeAudio):
                                if attr.voice:
                                    is_voice = True
                                    message_type = "audio"
                                    original_filename = "voice.ogg"
                                else:
                                    is_audio = True
                                    message_type = "audio"
                                    original_filename = attr.title or "audio.mp3"
                            elif isinstance(attr, DocumentAttributeFilename):
                                original_filename = attr.file_name

                        if not is_voice and not is_audio:
                            mime = doc.mime_type or ""
                            if mime.startswith("video/"):
                                message_type = "video"
                                original_filename = "video.mp4"
                            elif mime.startswith("image/"):
                                message_type = "image"
                                original_filename = "image.jpg"
                            else:
                                message_type = "document"

                        file_bytes = await tg.download_media(msg, bytes)
                        if file_bytes:
                            bucket, key = await sync_to_async(upload_file_to_s3)(
                                file_bytes,
                                prefix="telegram/media",
                                filename=original_filename
                            )
                            file_data = await sync_to_async(StoredFile.objects.create)(
                                bucket=bucket, key=key,
                                filename=original_filename,
                                content_type=doc.mime_type or "application/octet-stream",
                                size=len(file_bytes)
                            )
                            file_name = original_filename

                    elif isinstance(msg.media, MessageMediaPhoto):
                        message_type = "image"
                        original_filename = "photo.jpg"
                        file_bytes = await tg.download_media(msg, bytes)
                        if file_bytes:
                            bucket, key = await sync_to_async(upload_file_to_s3)(
                                file_bytes,
                                prefix="telegram/media",
                                filename=original_filename
                            )
                            file_data = await sync_to_async(StoredFile.objects.create)(
                                bucket=bucket, key=key,
                                filename=original_filename,
                                content_type="image/jpeg",
                                size=len(file_bytes)
                            )
                            file_name = original_filename

                await sync_to_async(Message.objects.create)(
                    client=db_client,
                    employee=None,
                    content=content,
                    message_type=message_type,
                    direction=direction,
                    channel="telegram",
                    telegram_message_id=msg.id,
                    file=file_data,
                    file_name=file_name,
                    is_sent=True,
                    is_read=True if direction == "incoming" else False,
                    telegram_date=msg.date,
                    raw_payload={
                        "channel": "telegram",
                        "message_id": msg.id,
                        "peer_id": int(telegram_id),
                        "date": msg.date.isoformat() if msg.date else None,
                        "media": type(msg.media).__name__ if msg.media else None,
                    },
                )
                imported_count += 1
                from celery import current_task
                current_task.update_state(
                    state='PROGRESS', meta={'current': imported_count, 'total': len(history)})
                logger.info(f"  [{direction}] {message_type} — {msg.id}")

            logger.info(f"✅ Imported {imported_count} messages for {telegram_id}")

    asyncio.run(do_import())

@shared_task
def send_reaction_task(message_uuid: str, emoji: str):
    """
    Отправить реакцию на сообщение: Telegram (через MTProto) или MAX (локально в БД).
    """
    try:
        msg = Message.objects.select_related('client').get(id=message_uuid)
    except Message.DoesNotExist:
        logger.error(f"send_reaction_task: message {message_uuid} not found")
        return

    if msg.channel == "telegram":
        _send_telegram_reaction(msg, emoji)
    elif msg.channel == "max":
        _send_max_reaction(msg, emoji)
    else:
        logger.warning(f"send_reaction_task: unknown channel {msg.channel}")


def _send_telegram_reaction(msg, emoji):
    """Отправляет реакцию через отдельный TelegramClient и сохраняет в БД."""
    if not msg.telegram_message_id or not msg.client.telegram_id:
        logger.warning(f"_send_telegram_reaction: no IDs for msg {msg.id}")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from apps.telegram.telegram_sender import get_telegram_client
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji, PeerUser

        async def _send():
            client = await get_telegram_client()
            try:
                await client(SendReactionRequest(
                    peer=PeerUser(msg.client.telegram_id),
                    msg_id=int(msg.telegram_message_id),
                    reaction=[ReactionEmoji(emoticon=emoji)],
                ))
            finally:
                await client.disconnect()

        loop.run_until_complete(_send())
        logger.info(f"✅ TG reaction {emoji} sent for {msg.id}")

        # Telegram не шлёт echo для собственных реакций — обновляем БД сами
        _save_reaction_locally(msg, emoji)

    except Exception as e:
        logger.exception(f"TG reaction error for {msg.id}: {e}")
    finally:
        loop.close()


def _save_reaction_locally(msg, emoji):
    """Сохраняет реакцию в БД и пушит обновление через WS."""
    msg.refresh_from_db(fields=["reactions"])
    reactions = msg.reactions.copy() if msg.reactions else {}
    reactions[emoji] = reactions.get(emoji, 0) + 1
    msg.reactions = reactions
    msg.save(update_fields=["reactions"])

    try:
        from apps.realtime.utils import push_message_reactions
        push_message_reactions(msg)
    except Exception as e:
        logger.warning(f"Reaction WS push error: {e}")

    logger.info(f"💟 Reaction {emoji} saved for {msg.id}: {reactions}")


def _send_max_reaction(msg, emoji):
    """MAX бот-API не поддерживает нативные реакции — сохраняем локально."""
    _save_reaction_locally(msg, emoji)


@shared_task
def send_max_message_task(message_id: int):
    from apps.maxchat.sender import send_max_message
    from django.conf import settings

    try:
        message = Message.objects.select_related('client', 'reply_to').get(id=message_id)
    except Message.DoesNotExist:
        return

    if not message.client.max_chat_id:
        return

    access_token = settings.MAX_ACCESS_TOKEN
    chat_id = str(message.client.max_chat_id)

    # Для MAX нет нативного reply, вставляем цитату в текст
    text_to_send = message.content or ""
    if message.reply_to_id and message.reply_to:
        quoted = (message.reply_to.content or "")[:100]
        text_to_send = f"💬 «{quoted}»\n\n{text_to_send}"

    file_bytes = None
    filename = None
    content_type = None

    if message.file:
        try:
            file_bytes = download_file_from_s3(message.file.bucket, message.file.key)
            filename = message.file.filename
            content_type = message.file.content_type
        except Exception as e:
            logger.exception(f"MAX: failed to download file for message {message_id}: {e}")

    try:
        ok, msg_id, err = send_max_message(
            access_token=access_token,
            chat_id=chat_id,
            text=text_to_send,
            file_bytes=file_bytes,
            filename=filename,
            message_type=message.message_type,
            content_type=content_type,
        )
        if ok:
            message.is_sent = True
            message.sent_at = timezone.now()
            message.max_message_id = msg_id or ""
            message.save(update_fields=["is_sent", "sent_at", "max_message_id"])
            logger.info(f"✅ MAX message {message_id} sent, max_id={msg_id}")
        else:
            logger.error(f"❌ MAX message {message_id} failed: {err}")
            try:
                from apps.realtime.utils import push_toast
                if message.employee and message.employee.user:
                    push_toast(message.employee.user, f"Ошибка MAX: {err}", level="error")
            except Exception:
                pass
    except Exception as e:
        logger.exception(f"❌ MAX task error for message {message_id}: {e}")
