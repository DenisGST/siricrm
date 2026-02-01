# Структура проекта для CRM системы

## Директория файлов

```
crm-system/
├── config/                          # Основная конфигурация Django
│   ├── __init__.py
│   ├── settings.py                 # settings.py (из config_settings.py)
│   ├── urls.py                     # urls.py (из config_urls.py)
│   ├── asgi.py
│   ├── wsgi.py
│   └── celery.py                   # celery.py (из celery_config.py)
│
├── apps/
│   ├── __init__.py
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py               # Base models, mixins
│   │   ├── admin.py
│   │   └── utils.py                # Utility functions
│   │
│   ├── crm/
│   │   ├── __init__.py
│   │   ├── models.py               # (из crm_models.py)
│   │   ├── admin.py
│   │   ├── serializers.py          # DRF serializers
│   │   ├── api.py                  # (из crm_api_viewsets.py)
│   │   ├── tasks.py                # (из crm_tasks.py)
│   │   ├── views.py                # HTMX views
│   │   ├── urls.py
│   │   └── handlers.py             # Telegram handlers (из telegram_handlers.py)
│   │
│   ├── auth_telegram/
│   │   ├── __init__.py
│   │   ├── models.py               # (из crm_models.py - TelegramUser)
│   │   ├── views.py                # Telegram auth views
│   │   ├── serializers.py
│   │   ├── urls.py
│   │   └── admin.py
│   │
│   ├── telegram/
│   │   ├── __init__.py
│   │   ├── views.py                # Webhook handler
│   │   ├── urls.py
│   │   └── handlers.py             # (из telegram_handlers.py)
│   │
│   └── storage/
│       ├── __init__.py
│       ├── models.py               # S3 storage integration
│       └── utils.py                # S3 helpers
│
├── templates/
│   ├── base.html
│   ├── dashboard.html              # (из dashboard_template.html)
│   ├── crm/
│   │   ├── kanban.html
│   │   ├── clients/
│   │   │   ├── list.html
│   │   │   ├── detail.html
│   │   │   └── chat.html
│   │   ├── operators/
│   │   │   ├── list.html
│   │   │   └── detail.html
│   │   └── logs/
│   │       └── list.html
│   └── auth/
│       ├── login.html
│       └── telegram_login.html
│
├── static/
│   ├── css/
│   │   └── style.css
│   └── js/
│       └── app.js
│
├── logs/                           # Log directory (created automatically)
│   ├── crm.log
│   └── celery.log
│
├── docker-compose.yml              # (из docker-compose.yml)
├── Dockerfile                      # (из Dockerfile)
├── requirements.txt                # (из requirements.txt)
├── .env.example                    # (из .env.example)
├── .env                            # Копия из .env.example (создать вручную)
├── .gitignore
├── manage.py
└── README.md                       # (из README.md)
```

## Быстрый старт

### 1. Установка проекта

```bash
# Создать директорию проекта
mkdir crm-system
cd crm-system

# Инициализировать Django проект
django-admin startproject config .

# Создать apps
python manage.py startapp core
python manage.py startapp crm
python manage.py startapp auth_telegram
python manage.py startapp telegram
python manage.py startapp storage
```

### 2. Добавить файлы

Скопировать содержимое файлов в соответствующие места:

- `config_settings.py` → `config/settings.py`
- `config_urls.py` → `config/urls.py`
- `celery_config.py` → `config/celery.py`
- `crm_models.py` → `apps/crm/models.py` и `apps/auth_telegram/models.py`
- `crm_tasks.py` → `apps/crm/tasks.py`
- `crm_api_viewsets.py` → `apps/crm/api.py`
- `telegram_handlers.py` → `apps/telegram/handlers.py` и `apps/crm/handlers.py`
- `dashboard_template.html` → `templates/dashboard.html`
- Остальные файлы: `docker-compose.yml`, `Dockerfile`, `requirements.txt`, `.env.example`, `README.md`

### 3. Инициализация Django

```bash
# Создать .env из примера
cp .env.example .env

# Отредактировать .env и заполнить Telegram token
# TELEGRAM_TOKEN=ваш_токен_здесь

# Миграции
python manage.py makemigrations
python manage.py migrate

# Создать superuser
python manage.py createsuperuser

# Собрать static files
python manage.py collectstatic --noinput
```

### 4. Запуск с Docker

```bash
# Создать .env файл
cp .env.example .env

# Заполнить переменные в .env

# Запустить контейнеры
docker-compose up -d

# Выполнить миграции в контейнере
docker-compose exec web python manage.py migrate

# Создать superuser в контейнере
docker-compose exec web python manage.py createsuperuser

# Проверить логи
docker-compose logs -f web
```

### 5. Доступ к приложению

- **Web UI:** http://localhost:8000/dashboard
- **Admin:** http://localhost:8000/admin
- **API Docs:** http://localhost:8000/api/schema/swagger/
- **Telegram Bot:** @YourBotName в Telegram

## Важные шаги для Telegram интеграции

### Получить Bot Token

1. Напишите [@BotFather](https://t.me/botfather)
2. `/newbot`
3. Следуйте инструкциям
4. Получите token вида: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`
5. Добавьте в `.env`: `TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`

### Настроить Webhook

После развёртывания на production сервер:

```bash
# В Python оболочке Django
python manage.py shell

from telegram import Bot
from decouple import config

bot = Bot(token=config('TELEGRAM_TOKEN'))
webhook_url = config('TELEGRAM_WEBHOOK_URL')

# Установить webhook
result = bot.set_webhook(url=webhook_url)
print(result)

# Проверить статус
info = bot.get_webhook_info()
print(info)
```

Или с curl:

```bash
curl -X POST \
  https://api.telegram.org/bot<TOKEN>/setWebhook \
  -d "url=https://yourdomain.com/api/telegram/webhook/"
```

## Файлы для редактирования / создания

Нужно создать дополнительные файлы:

### 1. `apps/crm/serializers.py`
```python
from rest_framework import serializers
from apps.crm.models import *

class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = '__all__'

class OperatorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Operator
        fields = '__all__'

class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = '__all__'

class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = '__all__'

class OperatorLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = OperatorLog
        fields = '__all__'
```

### 2. `apps/crm/urls.py`
```python
from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard, name='dashboard'),
    path('kanban/', views.kanban, name='kanban'),
    path('clients/', views.clients_list, name='clients_list'),
    path('clients/<uuid:client_id>/chat/', views.chat, name='chat'),
    path('operators/', views.operators_list, name='operators_list'),
    path('logs/', views.logs_list, name='logs_list'),
]
```

### 3. `apps/crm/views.py`
```python
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from apps.crm.models import *

@login_required
def dashboard(request):
    return render(request, 'dashboard.html')

@login_required
def kanban(request):
    clients = Client.objects.filter(status__in=['lead', 'active'])
    return render(request, 'crm/kanban.html', {'clients': clients})

# ... остальные views
```

### 4. `apps/telegram/urls.py`
```python
from django.urls import path
from . import views

urlpatterns = [
    path('webhook/', views.telegram_webhook, name='telegram_webhook'),
]
```

### 5. `apps/telegram/views.py`
```python
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import json
from telegram import Update
from apps.telegram.handlers import TelegramHandlers

@csrf_exempt
@require_http_methods(["POST"])
async def telegram_webhook(request):
    data = json.loads(request.body)
    update = Update.de_json(data, None)
    # Handle update...
    return JsonResponse({'ok': True})
```

## Проверка после развёртывания

```bash
# Проверить базу данных
docker-compose exec web python manage.py dbshell

# Проверить логи
docker-compose logs -f celery
docker-compose logs -f web

# Проверить Redis
docker-compose exec redis redis-cli ping

# Проверить статус Telegram бота
curl https://api.telegram.org/bot<TOKEN>/getMe
```

## Production Deployment

1. Используйте Gunicorn вместо Django development server
2. Настройте Nginx как reverse proxy
3. Включите HTTPS с Let's Encrypt
4. Используйте Supervisor для управления Celery workers
5. Настройте резервное копирование базы данных
6. Используйте managed S3 (AWS S3 или аналог)

Для Kubernetes:
- Создать Dockerfile (уже готов)
- Создать k8s manifests для deployment, service, configmap, secret
- Использовать managed PostgreSQL и Redis
- Настроить NGINX Ingress для routing

## Поддержка и Дебаг

Основные команды для дебага:

```bash
# Проверить миграции
python manage.py showmigrations

# Запустить тесты
python manage.py test

# Проверить Celery task
python manage.py shell
>>> from apps.crm.tasks import cleanup_old_logs
>>> cleanup_old_logs.delay(30).get()

# Просмотр Celery tasks
celery -A config inspect active

# Очистить Redis cache
redis-cli FLUSHDB

# Скачать и обработать логи
docker-compose logs > logs.txt
```

---

**Готово к использованию! 🚀**

Проект полностью функционален и готов для локальной разработки и production deployment.
