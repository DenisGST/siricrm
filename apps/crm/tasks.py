from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import Count, Q
import logging

from apps.crm.models import (
    Operator, Client, Message, OperatorLog, Department
)

logger = logging.getLogger(__name__)


@shared_task
def cleanup_old_logs(days=30):
    """Remove operator logs older than N days"""
    cutoff_date = timezone.now() - timedelta(days=days)
    deleted_count, _ = OperatorLog.objects.filter(timestamp__lt=cutoff_date).delete()
    logger.info(f"Deleted {deleted_count} old logs")
    return f"Deleted {deleted_count} logs older than {days} days"


@shared_task
def generate_daily_report():
    """Generate daily report for all departments"""
    today = timezone.now().date()
    report_data = {}
    
    try:
        for dept in Department.objects.filter(is_active=True):
            operators = dept.operators.filter(is_active=True)
            
            report_data[dept.name] = {
                'operators_count': operators.count(),
                'messages_sent': 0,
                'messages_received': 0,
                'new_clients': 0,
                'active_clients': 0,
                'operator_stats': []
            }
            
            for operator in operators:
                # Messages sent by operator today
                messages_sent = Message.objects.filter(
                    operator=operator,
                    direction='outgoing',
                    created_at__date=today
                ).count()
                
                # Messages received by operator's clients
                messages_received = Message.objects.filter(
                    client__assigned_operator=operator,
                    direction='incoming',
                    created_at__date=today
                ).count()
                
                # Logs for operator today
                actions = OperatorLog.objects.filter(
                    operator=operator,
                    timestamp__date=today
                )
                
                report_data[dept.name]['messages_sent'] += messages_sent
                report_data[dept.name]['messages_received'] += messages_received
                
                report_data[dept.name]['operator_stats'].append({
                    'operator': operator.user.get_full_name(),
                    'messages_sent': messages_sent,
                    'messages_received': messages_received,
                    'actions_count': actions.count(),
                    'clients_count': operator.clients.count(),
                })
            
            # New clients for department
            new_clients = Client.objects.filter(
                assigned_operator__department=dept,
                created_at__date=today
            ).count()
            report_data[dept.name]['new_clients'] = new_clients
            
            # Active clients
            active_clients = Client.objects.filter(
                assigned_operator__department=dept,
                status='active'
            ).count()
            report_data[dept.name]['active_clients'] = active_clients
        
        logger.info(f"Generated daily report: {report_data}")
        return report_data
        
    except Exception as e:
        logger.error(f"Error generating daily report: {e}")
        return {'error': str(e)}


@shared_task
def sync_operator_status():
    """Check and update operator online status"""
    timeout_minutes = 5  # Mark as offline if no activity for 5 minutes
    cutoff_time = timezone.now() - timedelta(minutes=timeout_minutes)
    
    try:
        # Get all online operators
        online_operators = Operator.objects.filter(is_online=True)
        
        for operator in online_operators:
            # Check last action time
            last_action = OperatorLog.objects.filter(
                operator=operator
            ).order_by('-timestamp').first()
            
            if last_action and last_action.timestamp < cutoff_time:
                # Mark as offline
                operator.is_online = False
                operator.save(update_fields=['is_online'])
                logger.info(f"Marked {operator} as offline due to inactivity")
        
        return f"Updated status for {len(online_operators)} operators"
        
    except Exception as e:
        logger.error(f"Error syncing operator status: {e}")
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
        if message.operator:
            text = f"{message.operator.user.get_full_name()}: {message.content}"
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
    """Reassign clients based on operator workload"""
    try:
        # Get all clients without assigned operator
        unassigned_clients = Client.objects.filter(
            assigned_operator__isnull=True,
            status__in=['lead', 'active']
        )
        
        for client in unassigned_clients:
            # Find operator with least clients in their department
            if client.assigned_operator and client.assigned_operator.department:
                dept = client.assigned_operator.department
            else:
                # Get department with least load
                dept = Department.objects.annotate(
                    operator_count=Count('operators'),
                    total_clients=Count('operators__clients')
                ).order_by('total_clients').first()
            
            if dept:
                operator = Operator.objects.filter(
                    department=dept,
                    is_active=True
                ).annotate(
                    client_count=Count('clients')
                ).order_by('client_count').first()
                
                if operator:
                    client.assigned_operator = operator
                    client.save(update_fields=['assigned_operator'])
                    
                    OperatorLog.objects.create(
                        operator=operator,
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
def generate_operator_stats(operator_id, start_date=None, end_date=None):
    """Generate detailed statistics for an operator"""
    try:
        from django.db.models import Count
        
        operator = Operator.objects.get(id=operator_id)
        
        if not start_date:
            start_date = timezone.now() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now()
        
        stats = {
            'operator': operator.user.get_full_name(),
            'period': f"{start_date.date()} to {end_date.date()}",
            'total_messages': Message.objects.filter(
                operator=operator,
                created_at__range=[start_date, end_date]
            ).count(),
            'clients_assigned': operator.clients.count(),
            'messages_by_type': {},
            'daily_activity': {},
        }
        
        # Group messages by type
        messages_by_type = Message.objects.filter(
            operator=operator,
            created_at__range=[start_date, end_date]
        ).values('message_type').annotate(count=Count('id'))
        
        for item in messages_by_type:
            stats['messages_by_type'][item['message_type']] = item['count']
        
        # Daily activity breakdown
        daily_data = OperatorLog.objects.filter(
            operator=operator,
            timestamp__range=[start_date, end_date]
        ).values('timestamp__date').annotate(count=Count('id'))
        
        for item in daily_data:
            stats['daily_activity'][str(item['timestamp__date'])] = item['count']
        
        logger.info(f"Generated stats for {operator}: {stats}")
        return stats
        
    except Operator.DoesNotExist:
        logger.error(f"Operator {operator_id} not found")
        return {'error': 'Operator not found'}
    except Exception as e:
        logger.error(f"Error generating operator stats: {e}")
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
