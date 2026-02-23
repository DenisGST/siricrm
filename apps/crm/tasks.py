# apps/crm/tasks.py

from datetime import timedelta
import logging
import asyncio

from celery import shared_task
from django.conf import settings
from django.utils import timezone
from django.db.models import Count

from telegram import Bot

from apps.crm.models import Client, Message
from apps.core.models import Employee, EmployeeLog, Department
from apps.files.s3_utils import download_file_from_s3

logger = logging.getLogger(__name__)


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
            employees = dept.employee.filter(is_active=True)
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
                    client__assigned_employee=employee,
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
                        "clients_count": employee.clients.count(),
                    }
                )

            new_clients = Client.objects.filter(
                assigned_employee__department=dept,
                created_at__date=today,
            ).count()
            report_data[dept.name]["new_clients"] = new_clients

            active_clients = Client.objects.filter(
                assigned_employee__department=dept,
                status="active",
            ).count()
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


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_telegram_message_async(self, message_id: str):
    """
    Асинхронная отправка сообщения в Telegram:
    - текст -> send_message
    - файл -> скачиваем из S3 по bucket/key и отправляем байты.
    """
    try:
        msg = Message.objects.select_related("client", "file").get(id=message_id)
    except Message.DoesNotExist:
        logger.error("Message %s not found", message_id)
        return {"error": "Message not found"}

    client = msg.client
    if not client.telegram_id:
        logger.error("Client %s has no telegram_id", client.id)
        return {"error": "No telegram_id"}

    async def _send():
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        chat_id = client.telegram_id

        if msg.message_type == "text":
            return await bot.send_message(chat_id=chat_id, text=msg.content or "")

        stored = msg.file  # StoredFile
        if not stored:
            logger.error("Message %s has no StoredFile for type %s", msg.id, msg.message_type)
            return None

        # скачиваем исходный файл из S3
        file_bytes = download_file_from_s3(stored.bucket, stored.key)

        if msg.message_type == "audio":
            return await bot.send_voice(chat_id=chat_id, voice=file_bytes)
        elif msg.message_type == "video":
            return await bot.send_video(chat_id=chat_id, video=file_bytes)
        elif msg.message_type == "image":
            return await bot.send_photo(
                chat_id=chat_id,
                photo=file_bytes,
                caption=msg.content or None,
            )
        else:
            return await bot.send_document(
                chat_id=chat_id,
                document=file_bytes,
                filename=stored.filename,
                caption=msg.content or None,
            )

    try:
        sent = asyncio.run(_send())
    except Exception as exc:
        logger.error("Telegram send failed for msg %s: %s", message_id, exc)
        # повторяем через 10 секунд, макс 3 раза
        raise self.retry(exc=exc)

    if sent:
        msg.telegram_message_id = sent.message_id
        msg.save(update_fields=["telegram_message_id"])
        logger.info(
            "Sent Telegram message to client %s (chat_id=%s, msg_id=%s)",
            client.id,
            client.telegram_id,
            sent.message_id,
        )
        return f"Message sent to Telegram (ID: {sent.message_id})"

    return {"error": "Nothing sent"}

@shared_task
def reassign_clients_by_load():
    try:
        unassigned_clients = Client.objects.filter(
            assigned_employee__isnull=True,
            status__in=["lead", "active"],
        )
        for client in unassigned_clients:
            dept = (
                client.assigned_employee.department
                if client.assigned_employee and client.assigned_employee.department
                else Department.objects.annotate(
                    employee_count=Count("employees"),
                    total_clients=Count("employees__clients"),
                )
                .order_by("total_clients")
                .first()
            )
            if not dept:
                continue
            employee = (
                Employee.objects.filter(department=dept, is_active=True)
                .annotate(client_count=Count("clients"))
                .order_by("client_count")
                .first()
            )
            if employee:
                client.assigned_employee = employee
                client.save(update_fields=["assigned_employee"])
                EmployeeLog.objects.create(
                    employee=employee,
                    action="client_assigned",
                    description=f"Клиент {client} переназначен системой",
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
            "clients_assigned": employee.clients.count(),
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
