# CRM System with Telegram Integration

Полнофункциональная CRM-система для общения операторов с клиентами через Telegram.

## Быстрый старт

### Подготовка

1. **Клонируйте проект:**
```bash
git clone <repo-url>
cd crm-system
```

2. **Создайте `.env` файл в корне проекта:**
```bash
cp .env.example .env
```

3. **Заполните переменные окружения:**
```
TELEGRAM_TOKEN=your_bot_token_here
TELEGRAM_WEBHOOK_URL=https://yourdomain.com/api/telegram/webhook/
AWS_ACCESS_KEY_ID=your_aws_key
AWS_SECRET_ACCESS_KEY=your_aws_secret
AWS_STORAGE_BUCKET_NAME=your_bucket_name
AWS_S3_REGION_NAME=us-east-1
```

### Получение Telegram Bot Token

1. Напишите [@BotFather](https://t.me/botfather) в Telegram
2. Команда: `/newbot`
3. Следуйте инструкциям, получите token
4. Установите webhook: `/setwebhook https://yourdomain.com/api/telegram/webhook/`

### Запуск

```bash
# С Docker Compose
docker-compose up -d

# Или локально (требует PostgreSQL, Redis)
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver

# В другом терминале запустите Celery
celery -A config worker -l info
```

### Доступ

- **Django Admin:** http://localhost:8000/admin
- **CRM Dashboard:** http://localhost:8000/dashboard
- **API Docs:** http://localhost:8000/api/schema/swagger/

## Структура проекта

```
crm-system/
├── config/                 # Основная конфигурация Django
│   ├── settings.py        # Настройки проекта
│   ├── urls.py           # Маршруты
│   ├── asgi.py          # ASGI для WebSocket
│   └── wsgi.py          # WSGI для production
├── apps/
│   ├── core/            # Базовые модели и утилиты
│   ├── crm/             # Основное CRM приложение
│   │   ├── models.py    # Department, Operator, Client, Message, Log
│   │   ├── views.py     # Django views и HTMX endpoints
│   │   ├── api.py       # REST API endpoints
│   │   ├── tasks.py     # Celery tasks
│   │   └── handlers.py  # Telegram handlers
│   ├── auth/            # Аутентификация через Telegram
│   │   ├── views.py     # Telegram auth views
│   │   └── models.py    # TelegramUser model
│   └── storage/         # S3 интеграция
├── templates/           # HTML шаблоны с HTMX
├── static/             # CSS (daisyUI), JavaScript
├── docker-compose.yml  # Конфигурация контейнеров
├── Dockerfile         # Docker image для приложения
├── requirements.txt   # Python зависимости
└── manage.py         # Django CLI

```

## Основные компоненты

### Models

#### Department (Отдел)
- name: Имя отдела
- description: Описание
- manager: Менеджер отдела
- created_at, updated_at

#### Operator (Оператор)
- user: ForeignKey к User
- telegram_id: ID в Telegram
- department: ForeignKey к Department
- is_active: Статус активности
- joined_at, last_seen

#### Client (Клиент)
- telegram_id: Уникальный ID в Telegram
- first_name, last_name, username
- phone, email
- assigned_operator: Оператор, работающий с клиентом
- status: lead, active, inactive, closed
- created_at, updated_at

#### Message (Сообщение)
- sender: User (оператор или система)
- client: ForeignKey к Client
- content: Текст сообщения
- message_type: text, file, image, system
- telegram_message_id: ID сообщения в Telegram
- created_at

#### OperatorLog (Логирование)
- operator: ForeignKey к Operator
- action: login, logout, message_sent, client_assigned, status_changed
- description: Дополнительная информация
- ip_address: IP адрес
- timestamp

### API Endpoints

```
GET    /api/operators/                    # Список операторов
GET    /api/operators/{id}/               # Детали оператора
GET    /api/clients/                      # Список клиентов
POST   /api/clients/                      # Создание клиента
GET    /api/messages/?client_id=X         # Сообщения клиента
POST   /api/messages/                     # Отправить сообщение
GET    /api/logs/?operator_id=X&date=Y   # Логи операторов
POST   /api/departments/                  # Управление отделами
```

### HTMX Endpoints

```
GET    /dashboard/                        # Главная панель
GET    /kanban/                           # Доска Kanban
GET    /clients/                          # Таблица клиентов
GET    /chat/{client_id}/                 # Чат с клиентом
POST   /chat/{client_id}/send/            # Отправить сообщение
GET    /logs/                             # Логи операторов
GET    /operators/                        # Управление операторами
```

### Telegram Integration

**Webhook:** `POST /api/telegram/webhook/`

**Обработчики:**
- `/start` - Регистрация оператора
- `/help` - Справка
- Текстовые сообщения от клиентов → сохраняются в БД
- Фото/документы → загружаются в S3

### Celery Tasks

```python
@task
def sync_telegram_updates()
    # Синхронизация обновлений из Telegram

@task
def send_message_to_telegram(message_id)
    # Отправка сообщения в Telegram

@task
def generate_report(operator_id, date_from, date_to)
    # Генерация отчета по оператору

@task
def cleanup_old_logs(days=30)
    # Удаление старых логов
```

## Настройка S3

### AWS S3

```python
# .env
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
AWS_STORAGE_BUCKET_NAME=my-crm-bucket
AWS_S3_REGION_NAME=eu-west-1
```

### MinIO (локальная разработка)

```bash
# Запустить MinIO в Docker
docker run -p 9000:9000 -p 9001:9001 minio/minio server /data --console-address ":9001"

# .env
AWS_STORAGE_BUCKET_NAME=crm-dev
AWS_S3_ENDPOINT_URL=http://minio:9000
```

## Аутентификация через Telegram

1. Оператор нажимает кнопку "Login with Telegram" на сайте
2. Система генерирует уникальный код
3. Оператор отправляет код боту: `/auth CODE_HERE`
4. Бот проверяет код и подтверждает вход
5. Сессия создается на сайте

```
Сайт → Генерирует код → Бот проверяет → Сайт логирует оператора
```

## Разработка

### Создание миграций

```bash
python manage.py makemigrations
python manage.py migrate
```

### Запуск тестов

```bash
python manage.py test
```

### Создание superuser

```bash
python manage.py createsuperuser
```

### Lint кода

```bash
flake8 apps/
black apps/
```

## Production Deployment

### Подготовка

1. **Настройте环境 переменные:**
```bash
DEBUG=False
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
SECRET_KEY=your-secure-secret-key
```

2. **Используйте Gunicorn + Nginx:**
```bash
gunicorn config.wsgi:application --workers 4 --bind 0.0.0.0:8000
```

3. **Настройте HTTPS:**
- Let's Encrypt с Certbot
- Перенаправление HTTP → HTTPS в Nginx

4. **Celery в production:**
```bash
celery -A config worker -c 4 -l info --without-gossip --without-mingle --without-heartbeat
celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

## Troubleshooting

### Проблема: Webhook не работает
- Проверьте `TELEGRAM_WEBHOOK_URL` в .env
- Убедитесь, что домен доступен извне
- Логи в Telegram Bot API: `getWebhookInfo`

### Проблема: S3 не загружает файлы
- Проверьте AWS credentials в .env
- Убедитесь, что bucket существует
- Проверьте permissions в AWS IAM

### Проблема: Celery задачи не выполняются
- Проверьте, что Redis запущен
- Проверьте логи: `docker-compose logs celery`
- Убедитесь, что `CELERY_BROKER_URL` правильная

## Поддержка

Для вопросов и баг-репортов создавайте Issues в репозитории.

---

**Автор:** CRM Development Team  
**Лицензия:** MIT
