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


@shared_task
def send_telegram_message_task(message_id):
    """
    Отправка сообщения через Telegram userbot с поддержкой медиа
    """
    from apps.crm.models import Message
    from django.utils import timezone
    
    try:
        message = Message.objects.select_related('client', 'file').get(id=message_id)
        
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
        
        # Отправляем через userbot
        result = asyncio.run(send_telegram_message(**send_params))
        
        if result['success']:
            # Обновляем сообщение
            message.is_sent = True
            message.telegram_message_id = result['message_id']
            message.sent_at = timezone.now()
            message.save(update_fields=['is_sent', 'telegram_message_id', 'sent_at'])
            
            logger.info(f"✅ Task: Message {message_id} sent successfully, telegram_id={result['message_id']}")
            
            # Обновляем UI через WebSocket
            try:
                from apps.realtime.utils import push_chat_message
                push_chat_message(message)
            except Exception as e:
                logger.warning(f"Failed to push websocket update: {e}")
        else:
            logger.error(f"❌ Task: Failed to send message {message_id}: {result['error']}")
            
    except Message.DoesNotExist:
        logger.error(f"❌ Task: Message {message_id} not found")
    except Exception as e:
        logger.exception(f"❌ Task error for message {message_id}: {e}")


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
