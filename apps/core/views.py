from django.shortcuts import render, redirect
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from apps.crm.models import Message, Client
from apps.core.models import Employee
from django.utils import timezone
from datetime import timedelta
import psutil
import os
import re


def is_superuser(user):
    return user.is_superuser


@user_passes_test(is_superuser)
def monitoring_dashboard(request):
    """Панель мониторинга для администраторов"""
    context = {
        'page_title': 'Мониторинг системы',
    }
    return render(request, 'monitoring/dashboard.html', context)


@user_passes_test(is_superuser)
def monitoring_api(request):
    """API для получения данных мониторинга"""
    try:
        # Системные метрики
        cpu_percent = psutil.cpu_percent(interval=0.5)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Сетевые метрики
        net_io = psutil.net_io_counters()
        
        # Бизнес-метрики
        now = timezone.now()
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(days=1)
        
        active_clients = Client.objects.filter(status='active').count()
        total_clients = Client.objects.count()
        leads_count = Client.objects.filter(status='lead').count()
        
        unread_messages = Message.objects.filter(
            is_read=False, 
            direction='incoming'
        ).count()
        
        messages_last_hour = Message.objects.filter(
            created_at__gte=hour_ago
        ).count()
        
        messages_last_day = Message.objects.filter(
            created_at__gte=day_ago
        ).count()
        
        online_employees = Employee.objects.filter(is_online=True).count()
        total_employees = Employee.objects.count()
        
        # Последние ошибки из логов
        errors = parse_last_errors('/app/logs/crm.log', limit=30)
        userbot_errors = parse_last_errors('/app/logs/userbot.log', limit=30)
        
        data = {
            'system': {
                'cpu_percent': round(cpu_percent, 1),
                'memory_percent': round(memory.percent, 1),
                'memory_used_gb': round(memory.used / (1024**3), 2),
                'memory_total_gb': round(memory.total / (1024**3), 2),
                'disk_percent': round(disk.percent, 1),
                'disk_used_gb': round(disk.used / (1024**3), 2),
                'disk_total_gb': round(disk.total / (1024**3), 2),
                'network_sent_mb': round(net_io.bytes_sent / (1024**2), 2),
                'network_recv_mb': round(net_io.bytes_recv / (1024**2), 2),
            },
            'business': {
                'active_clients': active_clients,
                'total_clients': total_clients,
                'leads_count': leads_count,
                'unread_messages': unread_messages,
                'messages_last_hour': messages_last_hour,
                'messages_last_day': messages_last_day,
                'online_employees': online_employees,
                'total_employees': total_employees,
            },
            'errors': {
                'django': errors,
                'userbot': userbot_errors,
            },
            'timestamp': now.isoformat(),
        }
        
        return JsonResponse(data)
        
    except Exception as e:
        return JsonResponse({
            'error': str(e)
        }, status=500)


def parse_last_errors(log_file, limit=30):
    """Парсинг последних ошибок из лог-файла"""
    errors = []
    
    if not os.path.exists(log_file):
        return errors
    
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        # Ищем строки с ERROR или CRITICAL
        error_pattern = re.compile(r'(ERROR|CRITICAL|Exception|Traceback)')
        
        current_error = None
        for line in reversed(lines):
            if error_pattern.search(line):
                if current_error is None:
                    current_error = {'lines': [], 'timestamp': None}
                
                current_error['lines'].insert(0, line.strip())
                
                # Пытаемся извлечь timestamp
                timestamp_match = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                if timestamp_match:
                    current_error['timestamp'] = timestamp_match.group(1)
            else:
                if current_error:
                    errors.append({
                        'text': '\n'.join(current_error['lines']),
                        'timestamp': current_error['timestamp'] or 'Unknown',
                    })
                    current_error = None
                    
                    if len(errors) >= limit:
                        break
        
        # Добавляем последнюю ошибку если есть
        if current_error:
            errors.append({
                'text': '\n'.join(current_error['lines']),
                'timestamp': current_error['timestamp'] or 'Unknown',
            })
        
    except Exception as e:
        errors.append({
            'text': f'Error reading log file: {str(e)}',
            'timestamp': 'Unknown',
        })
    
    return errors[:limit]
