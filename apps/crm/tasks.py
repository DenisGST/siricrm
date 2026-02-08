from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import Count, Q
import logging

from apps.crm.models import (
    Client, Message
)
from apps.core.models import (
    Emploee, EmployeeLog, Department
)

logger = logging.getLogger(__name__)


@shared_task
def cleanup_old_logs(days=30):
    """Remove employee logs older than N days"""
    cutoff_date = timezone.now() - timedelta(days=days)
    deleted_count, _ = EmployeeLog.objects.filter(timestamp__lt=cutoff_date).delete()
    logger.info(f"Deleted {deleted_count} old logs")
    return f"Deleted {deleted_count} logs older than {days} days"


@shared_task
def generate_daily_report():
    """Generate daily report for all departments"""
    today = timezone.now().date()
    report_data = {}
    
    try:
        for dept in Department.objects.filter(is_active=True):
            employees = dept.employee.filter(is_active=True)
            
            report_data[dept.name] = {
                'employees_count': employee.count(),
                'messages_sent': 0,
                'messages_received': 0,
                'new_clients': 0,
                'active_clients': 0,
                'employee_stats': []
            }
            
            for employee in employees:
                # Messages sent by employee today
                messages_sent = Message.objects.filter(
                    employee=employee,
                    direction='outgoing',
                    created_at__date=today
                ).count()
                
                # Messages received by employee's clients
                messages_received = Message.objects.filter(
                    client__assigned_employee=employee,
                    direction='incoming',
                    created_at__date=today
                ).count()
                
                # Logs for employee today
                actions = EmployeeLog.objects.filter(
                    employee=employee,
                    timestamp__date=today
                )
                
                report_data[dept.name]['messages_sent'] += messages_sent
                report_data[dept.name]['messages_received'] += messages_received
                
                report_data[dept.name]['oemployee_stats'].append({
                    'employee': employee.user.get_full_name(),
                    'messages_sent': messages_sent,
                    'messages_received': messages_received,
                    'actions_count': actions.count(),
                    'clients_count': employee.clients.count(),
                })
            
            # New clients for department
            new_clients = Client.objects.filter(
                assigned_employee__department=dept,
                created_at__date=today
            ).count()
            report_data[dept.name]['new_clients'] = new_clients
            
            # Active clients
            active_clients = Client.objects.filter(
                assigned_employee__department=dept,
                status='active'
            ).count()
            report_data[dept.name]['active_clients'] = active_clients
        
        logger.info(f"Generated daily report: {report_data}")
        return report_data
        
    except Exception as e:
        logger.error(f"Error generating daily report: {e}")
        return {'error': str(e)}


@shared_task
def sync_employee_status():
    """Check and update employee online status"""
    timeout_minutes = 5  # Mark as offline if no activity for 5 minutes
    cutoff_time = timezone.now() - timedelta(minutes=timeout_minutes)
    
    try:
        # Get all online employees
        online_employees = Employee.objects.filter(is_online=True)
        
        for employee in employees_employees:
            # Check last action time
            last_action = EmployeeLog.objects.filter(
                employee=employee
            ).order_by('-timestamp').first()
            
            if last_action and last_action.timestamp < cutoff_time:
                # Mark as offline
                employee.is_online = False
                employee.save(update_fields=['is_online'])
                logger.info(f"Marked {employee} as offline due to inactivity")
        
        return f"Updated status for {len(online_employees)} employees"
        
    except Exception as e:
        logger.error(f"Error syncing employee status: {e}")
        return {'error': str(e)}


@shared_task
def send_message_to_telegram(message_id):
    """Send stored message to Telegram (for integration)"""
    try:
        from telegram import Bot
        from decouple import config
        
        message = Message.objects.get(id=message_id)
        bot = Bot(token=config('TELEGRAM_TOKEN'))
        
        # Send message to client in Telegram
        if message.employee:
            text = f"{message.employee.user.get_full_name()}: {message.content}"
        else:
            text = message.content
        
        result = await bot.send_message(
            chat_id=message.client.telegram_id,
            text=text
        )
        
        message.telegram_message_id = result.message_id
        message.save(update_fields=['telegram_message_id'])
        
        logger.info(f"Sent message {message_id} to Telegram")
        return f"Message sent to Telegram (ID: {result.message_id})"
        
    except Message.DoesNotExist:
        logger.error(f"Message {message_id} not found")
        return {'error': 'Message not found'}
    except Exception as e:
        logger.error(f"Error sending message to Telegram: {e}")
        return {'error': str(e)}


@shared_task
def reassign_clients_by_load():
    """Reassign clients based on employee workload"""
    try:
        # Get all clients without assigned employee
        unassigned_clients = Client.objects.filter(
            assigned_employee__isnull=True,
            status__in=['lead', 'active']
        )
        
        for client in unassigned_clients:
            # Find employee with least clients in their department
            if client.assigned_employee and client.assigned_employee.department:
                dept = client.assigned_employee.department
            else:
                # Get department with least load
                dept = Department.objects.annotate(
                    employee_count=Count('employees'),
                    total_clients=Count('employees__clients')
                ).order_by('total_clients').first()
            
            if dept:
                employee = Employee.objects.filter(
                    department=dept,
                    is_active=True
                ).annotate(
                    client_count=Count('clients')
                ).order_by('client_count').first()
                
                if employee:
                    client.assigned_employee = employee
                    client.save(update_fields=['assigned_employee'])
                    
                    EmployeeLog.objects.create(
                        employee=employee,
                        action='client_assigned',
                        description=f"Клиент {client} переназначен системой",
                        client=client,
                    )
        
        logger.info(f"Reassigned {unassigned_clients.count()} clients")
        return f"Reassigned {unassigned_clients.count()} clients"
        
    except Exception as e:
        logger.error(f"Error reassigning clients: {e}")
        return {'error': str(e)}


@shared_task
def generate_employee_stats(employee_id, start_date=None, end_date=None):
    """Generate detailed statistics for an employee"""
    try:
        from django.db.models import Count
        
        employee = Employee.objects.get(id=employee_id)
        
        if not start_date:
            start_date = timezone.now() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now()
        
        stats = {
            'employee': employee.user.get_full_name(),
            'period': f"{start_date.date()} to {end_date.date()}",
            'total_messages': Message.objects.filter(
                employee=employee,
                created_at__range=[start_date, end_date]
            ).count(),
            'clients_assigned': employee.clients.count(),
            'messages_by_type': {},
            'daily_activity': {},
        }
        
        # Group messages by type
        messages_by_type = Message.objects.filter(
            employee=employee,
            created_at__range=[start_date, end_date]
        ).values('message_type').annotate(count=Count('id'))
        
        for item in messages_by_type:
            stats['messages_by_type'][item['message_type']] = item['count']
        
        # Daily activity breakdown
        daily_data = EmployeeLog.objects.filter(
            employee=employee,
            timestamp__range=[start_date, end_date]
        ).values('timestamp__date').annotate(count=Count('id'))
        
        for item in daily_data:
            stats['daily_activity'][str(item['timestamp__date'])] = item['count']
        
        logger.info(f"Generated stats for {employee}: {stats}")
        return stats
        
    except Employee.DoesNotExist:
        logger.error(f"Employee {employee_id} not found")
        return {'error': 'Employee not found'}
    except Exception as e:
        logger.error(f"Error generating employee stats: {e}")
        return {'error': str(e)}


@shared_task
def archive_old_messages(days=90):
    """Archive messages older than N days (for performance)"""
    try:
        cutoff_date = timezone.now() - timedelta(days=days)
        old_messages = Message.objects.filter(created_at__lt=cutoff_date)
        
        # In production, you'd move these to archive storage
        archived_count = old_messages.count()
        
        # For now, just log them
        logger.info(f"Archived {archived_count} messages older than {days} days")
        
        return f"Archived {archived_count} messages"
        
    except Exception as e:
        logger.error(f"Error archiving messages: {e}")
        return {'error': str(e)}
