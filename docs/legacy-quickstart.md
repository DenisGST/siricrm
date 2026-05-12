# CRM System - Quick Start Guide

## 📦 Что вы получили

Полнофункциональная CRM-система с:
- ✅ Django 5.2 + DRF для backend
- ✅ HTMX + daisyUI для красивого frontend
- ✅ Celery + Redis для async tasks
- ✅ PostgreSQL для хранения данных
- ✅ python-telegram-bot для интеграции Telegram
- ✅ AWS S3 (или MinIO) для хранения файлов
- ✅ Docker Compose для локальной разработки
- ✅ Полное логирование действий операторов
- ✅ Kanban доска для управления клиентами
- ✅ Real-time статистика операторов

## 🚀 Быстрый старт (5 минут)

### Шаг 1: Подготовка

```bash
# Клонировать репозиторий
git clone <your-repo>
cd crm-system

# Создать .env файл
cp .env.example .env
```

### Шаг 2: Добавить Telegram Token

Отредактируйте `.env`:
```env
TELEGRAM_TOKEN=ваш_токен_от_botfather
TELEGRAM_WEBHOOK_URL=https://yourdomain.com/api/telegram/webhook/
```

### Шаг 3: Запустить Docker

```bash
# Запустить все контейнеры
docker-compose up -d

# Ждать, пока контейнеры готовы (обычно 30-60 сек)
sleep 30

# Выполнить миграции
docker-compose exec web python manage.py migrate

# Создать админа
docker-compose exec web python manage.py createsuperuser
```

### Шаг 4: Открыть в браузере

- 🌐 Dashboard: http://localhost:8000/dashboard
- 🔐 Admin: http://localhost:8000/admin (логин admin)
- 📚 API Docs: http://localhost:8000/api/schema/swagger/

## 📋 Основные компоненты

### Models

| Model | Описание |
|-------|---------|
| **Department** | Отдел (Sales, Support, Marketing и т.д.) |
| **Operator** | Оператор/сотрудник, работает с клиентами |
| **Client** | Клиент, общается с операторами через Telegram |
| **Message** | Сообщение в диалоге оператор-клиент |
| **OperatorLog** | Логирование всех действий оператора |
| **TelegramUser** | Связь между Django User и Telegram ID |

### API Endpoints

```
GET    /api/departments/          - Список отделов
GET    /api/operators/             - Список операторов
GET    /api/clients/               - Список клиентов
GET    /api/messages/              - Список сообщений
GET    /api/logs/                  - Логи операторов

POST   /api/operators/             - Создать оператора
POST   /api/clients/               - Создать клиента
POST   /api/messages/              - Отправить сообщение

PUT    /api/operators/{id}/        - Обновить оператора
PUT    /api/clients/{id}/          - Обновить клиента

DELETE /api/operators/{id}/        - Удалить оператора
DELETE /api/clients/{id}/          - Удалить клиента
```

### HTMX Views

```
/dashboard/                  - Главная панель
/kanban/                     - Kanban доска
/clients/                    - Список клиентов
/chat/{client_id}/           - Чат с клиентом
/operators/                  - Управление операторами
/logs/                       - Логи операторов
```

## 🤖 Telegram Integration

### Для оператора

1. Оператор запускает бота: `/start`
2. Нажимает кнопку "Зарегистрироваться в CRM"
3. На сайте подтверждает свой Telegram ID
4. Готово! Теперь может принимать клиентов

### Для клиента

1. Клиент пишет сообщение боту
2. Система автоматически регистрирует клиента
3. Находит оператора с наименьшей нагрузкой
4. Назначает клиента этому оператору
5. Оператор получает уведомление

### Команды бота

```
/start    - Регистрация оператора
/help     - Справка по доступным командам
/status   - Ваш статус в системе
/clients  - Список ваших клиентов
```

## 🔒 Аутентификация

### Через Telegram

1. Нажать "Login with Telegram" на сайте
2. Пройти процесс Telegram auth
3. Бот подтверждает вход
4. Сессия создается на сайте

### Через Django Admin

1. Логин: `admin`
2. Пароль: (который вы создали командой `createsuperuser`)
3. Создать операторов в админке

## 📊 Celery Tasks

Автоматические задачи, запускаемые по расписанию:

```python
# Каждый день в 2:00 AM - очистить старые логи
cleanup_old_logs(days=30)

# Каждый день в 10:00 PM - сгенерировать отчет
generate_daily_report()

# Каждую минуту - синхронизировать статусы операторов
sync_operator_status()
```

### Запустить задачу вручную

```bash
# В контейнере Django
docker-compose exec web python manage.py shell

from apps.crm.tasks import cleanup_old_logs
cleanup_old_logs.delay(days=30).get()  # Wait for result
```

## 📁 Структура файлов

```
crm-system/
├── config/              # Django config
├── apps/
│   ├── core/           # Base models
│   ├── crm/            # Main CRM app
│   ├── auth_telegram/  # Telegram auth
│   ├── telegram/       # Telegram webhook
│   └── storage/        # S3 integration
├── templates/          # HTML templates
├── static/             # CSS, JS
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env                # Your secrets
├── manage.py
└── README.md
```

## 🔧 Частые операции

### Создать нового оператора

```bash
# Способ 1: Через админку
1. Откройте http://localhost:8000/admin/
2. Users -> Add User -> Заполните данные
3. Сохраните
4. Создайте Operator запись и привяжите его к User и Telegram ID

# Способ 2: Через API
curl -X POST http://localhost:8000/api/operators/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "user": 1,
    "telegram_id": 123456789,
    "department": "uuid-отдела"
  }'
```

### Получить список всех сообщений клиента

```bash
curl http://localhost:8000/api/messages/?client_id=<client_uuid>
```

### Отправить сообщение от оператора

```bash
curl -X POST http://localhost:8000/api/messages/ \
  -H "Content-Type: application/json" \
  -d '{
    "operator": "uuid",
    "client": "uuid",
    "content": "Привет! Как дела?",
    "message_type": "text",
    "direction": "outgoing"
  }'
```

### Посмотреть логи оператора

```bash
# За последние 7 дней
curl "http://localhost:8000/api/logs/?operator_id=<uuid>&timestamp__gte=2024-01-19"
```

### Изменить статус клиента

```bash
curl -X POST "http://localhost:8000/api/clients/<uuid>/change_status/" \
  -H "Content-Type: application/json" \
  -d '{"status": "active"}'
```

## 📈 Статистика и Отчеты

### Dashboard показывает

- 🟢 Активные операторы
- 👥 Активные клиенты
- 📬 Новые сообщения
- 📊 Лиды (leads)

### Для каждого оператора

- 📱 Количество клиентов
- 📧 Сообщений отправлено/получено
- ⏱️ Время ответа
- 📈 Тренды активности

### Для отдела

- 👨‍💼 Количество операторов
- 🟢 Онлайн операторов
- 👥 Всего клиентов
- 📊 Активных клиентов

## 🐛 Troubleshooting

### Контейнер не запускается

```bash
# Проверить логи
docker-compose logs web

# Перезапустить
docker-compose restart web

# Проверить статус
docker-compose ps
```

### Telegram webhook не работает

```bash
# Проверить webhook
curl -X GET https://api.telegram.org/bot<TOKEN>/getWebhookInfo

# Установить заново
python manage.py shell
from telegram import Bot
from decouple import config
bot = Bot(config('TELEGRAM_TOKEN'))
bot.set_webhook(url=config('TELEGRAM_WEBHOOK_URL'))
```

### Celery tasks не выполняются

```bash
# Проверить статус Celery
docker-compose exec celery celery -A config inspect active

# Посмотреть логи Celery
docker-compose logs -f celery

# Перезапустить worker
docker-compose restart celery
```

### База данных не инициализируется

```bash
# Проверить подключение
docker-compose exec db psql -U crm_user -d crm_db -c "SELECT 1"

# Пересоздать
docker-compose down
docker volume rm crm-system_postgres_data
docker-compose up -d
```

## 🚀 Production Deployment

### На Heroku

```bash
# 1. Создать app
heroku create your-crm-app

# 2. Добавить addons
heroku addons:create heroku-postgresql:standard-0
heroku addons:create heroku-redis:premium-0

# 3. Установить переменные
heroku config:set TELEGRAM_TOKEN=... DEBUG=False SECRET_KEY=...

# 4. Запушить код
git push heroku main

# 5. Миграции
heroku run python manage.py migrate

# 6. Создать admin
heroku run python manage.py createsuperuser
```

### На AWS

1. EC2 instance (t3.small minimum)
2. RDS PostgreSQL
3. ElastiCache Redis
4. S3 bucket для файлов
5. Application Load Balancer
6. Route53 для DNS

### На DigitalOcean / Linode

```bash
# Docker на сервере
curl -fsSL https://get.docker.com | sh

# Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose

# Клонировать проект
git clone <repo>
cd crm-system

# Создать .env
cp .env.example .env
# Отредактировать .env

# Запустить
docker-compose up -d

# Nginx reverse proxy настроить отдельно
# Let's Encrypt certbot для HTTPS
```

## 📚 Полезные ссылки

- 📖 [Django Documentation](https://docs.djangoproject.com/)
- 🔌 [DRF Documentation](https://www.django-rest-framework.org/)
- 🤖 [python-telegram-bot](https://python-telegram-bot.readthedocs.io/)
- 🚀 [HTMX Documentation](https://htmx.org/)
- 🎨 [daisyUI Documentation](https://daisyui.com/)
- 📦 [Docker Documentation](https://docs.docker.com/)
- ⏰ [Celery Documentation](https://docs.celeryproject.io/)

## 💡 Tips & Tricks

### Быстро найти клиента

```bash
GET /api/clients/?search=ivan
GET /api/clients/?search=+79991234567
GET /api/clients/?search=ivan@example.com
```

### Фильтровать по статусу

```bash
# Только лиды
GET /api/clients/?status=lead

# Активные клиенты
GET /api/clients/?status=active

# Закрытые
GET /api/clients/?status=closed
```

### Сортировка

```bash
# По последнему сообщению
GET /api/clients/?ordering=-last_message_at

# По дате создания
GET /api/clients/?ordering=created_at
```

### Пагинация

```bash
# По 100 на странице
GET /api/clients/?limit=100&offset=0

# Вторая страница
GET /api/clients/?limit=50&offset=50
```

### Экспорт данных

```bash
# Все сообщения в JSON
curl http://localhost:8000/api/messages/ > messages.json

# Все логи в JSON
curl http://localhost:8000/api/logs/ > logs.json
```

## 📞 Поддержка

Если возникли вопросы:
1. Проверьте логи: `docker-compose logs -f`
2. Смотрите README.md и DEPLOYMENT.md
3. Создавайте Issues в репозитории

---

**Готово! 🎉 Успешной разработки!**
