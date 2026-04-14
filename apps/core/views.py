from django.shortcuts import render
from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.core.cache import cache
from apps.crm.models import Message, Client
from apps.core.models import Employee
from django.utils import timezone
from datetime import datetime, timedelta
import psutil
import os
import re


LOG_FILES = {
    'django':   '/app/logs/crm.log',
    'userbot':  '/app/logs/userbot.log',
    'celery':   '/app/logs/celery.log',
    'maxbot':   '/app/logs/maxbot.log',
}


def is_superuser(user):
    return user.is_superuser


@user_passes_test(is_superuser)
def monitoring_dashboard(request):
    context = {'page_title': 'Мониторинг системы'}
    return render(request, 'monitoring/dashboard.html', context)


@user_passes_test(is_superuser)
def monitoring_api(request):
    """API для получения данных мониторинга"""
    try:
        # Системные метрики — cpu_percent(interval=None) не блокирует поток
        cpu_percent = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        net_io = psutil.net_io_counters()

        # Бизнес-метрики — кэшируем на 30 секунд
        now = timezone.now()
        business = cache.get('monitoring_business')
        if business is None:
            hour_ago = now - timedelta(hours=1)
            day_ago = now - timedelta(days=1)
            business = {
                'active_clients': Client.objects.filter(status='active').count(),
                'total_clients': Client.objects.count(),
                'leads_count': Client.objects.filter(status='lead').count(),
                'unread_messages': Message.objects.filter(is_read=False, direction='incoming').count(),
                'messages_last_hour': Message.objects.filter(created_at__gte=hour_ago).count(),
                'messages_last_day': Message.objects.filter(created_at__gte=day_ago).count(),
                'online_employees': Employee.objects.filter(is_online=True).count(),
                'total_employees': Employee.objects.count(),
            }
            cache.set('monitoring_business', business, 30)

        # Логи — кэшируем на 60 секунд, парсинг файлов дорогой
        errors = {}
        for log_key, log_path in LOG_FILES.items():
            cache_key = f'monitoring_errors_{log_key}'
            result = cache.get(cache_key)
            if result is None:
                cleared_at = cache.get(f'monitoring_cleared_{log_key}')
                result = parse_last_errors(log_path, limit=30, cleared_at=cleared_at)
                cache.set(cache_key, result, 60)
            errors[log_key] = result

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
            'business': business,
            'errors': errors,
            'timestamp': now.isoformat(),
        }

        return JsonResponse(data)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@require_POST
@user_passes_test(is_superuser)
def monitoring_clear_log(request):
    """Очищает файл лога (truncate) и сбрасывает кэш."""
    log_key = request.POST.get('log')
    if log_key not in LOG_FILES:
        return JsonResponse({'error': 'Unknown log'}, status=400)

    log_path = LOG_FILES[log_key]
    try:
        if os.path.exists(log_path):
            with open(log_path, 'w', encoding='utf-8') as f:
                f.truncate(0)
    except Exception as e:
        return JsonResponse({'error': f'Failed to clear: {e}'}, status=500)

    cache.delete(f'monitoring_errors_{log_key}')
    cache.delete(f'monitoring_cleared_{log_key}')

    return JsonResponse({'ok': True})


def parse_last_errors(log_file, limit=30, cleared_at=None):
    """Парсинг последних ошибок из лог-файла.

    cleared_at — строка 'YYYY-MM-DD HH:MM:SS'; ошибки старше неё скрываются.
    """
    errors = []

    if not os.path.exists(log_file):
        return errors

    cleared_dt = None
    if cleared_at:
        try:
            cleared_dt = datetime.strptime(cleared_at, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass

    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        error_pattern = re.compile(r'(ERROR|CRITICAL|Exception|Traceback)')
        ts_pattern = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

        current_error = None
        for line in reversed(lines):
            if error_pattern.search(line):
                if current_error is None:
                    current_error = {'lines': [], 'timestamp': None, 'dt': None}

                current_error['lines'].insert(0, line.strip())

                ts_match = ts_pattern.search(line)
                if ts_match and current_error['timestamp'] is None:
                    current_error['timestamp'] = ts_match.group(1)
                    try:
                        current_error['dt'] = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        pass
            else:
                if current_error:
                    dt = current_error.get('dt')
                    # Пропускаем ошибки до момента очистки
                    if cleared_dt and dt and dt <= cleared_dt:
                        current_error = None
                        continue

                    errors.append({
                        'text': '\n'.join(current_error['lines']),
                        'timestamp': current_error['timestamp'] or 'Unknown',
                    })
                    current_error = None

                    if len(errors) >= limit:
                        break

        if current_error:
            dt = current_error.get('dt')
            if not (cleared_dt and dt and dt <= cleared_dt):
                errors.append({
                    'text': '\n'.join(current_error['lines']),
                    'timestamp': current_error['timestamp'] or 'Unknown',
                })

    except Exception as e:
        errors.append({'text': f'Error reading log file: {str(e)}', 'timestamp': 'Unknown'})

    return errors[:limit]
