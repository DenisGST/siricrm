# Telegram интеграция (`apps/telegram/`)

Два разных Telegram-канала в одном приложении:
1. **Userbot** (Telethon) — обычный TG-аккаунт компании, через него идёт CRM-чат с клиентами (двусторонний).
2. **Lead bot** — отдельный обычный bot-account, читает уведомления о новых лидах из служебного канала.

## Userbot (Telethon) — `apps/telegram/userbot.py`

- Авторизация по строковой сессии (`TELEGRAM_SESSION_STRING`), API-ключи Telegram — `TELEGRAM_API_ID` + `TELEGRAM_API_HASH`, телефон — `TELEGRAM_PHONE` (для первичной авторизации).
- При пустых credentials gracefully выходит — на dev userbot часто не запускается.
- Поддержка **MTProxy** (опционально): `ConnectionTcpMTProxyRandomizedIntermediate` — через env `TELEGRAM_PROXY` (если Telegram заблокирован в стране). Без MTProxy сейчас держится через **WireGuard split-tunnel** (см. инфраструктурную секцию CLAUDE.md).
- `keep_connected()` — основной loop с автореконнектом каждые 5с при разрывах.
- Запуск как отдельный контейнер `userbot` в compose (см. `docker-compose.prod.yml`).

### Отправка из CRM

- `apps/telegram/telegram_sender.py:create_message_and_store_file()` — общий хелпер для CRM (используется и MAX, и WhatsApp views): создаёт `Message` с прикреплённым `StoredFile` (через S3) для исходящих.
- Сама отправка через userbot — в коде userbot.py.
- View: `apps/crm/views.telegram_send_message` (URL `clients/<uuid>/chat/send/`). Шаблон кнопки — `templates/crm/partials/telegram_chat_panel.html`, кнопка `#btn-send-telegram` — рядом с MAX/WhatsApp.

### Приём входящих

- Userbot сам слушает события Telegram (Telethon → `events.NewMessage`).
- Сохраняет в `Message(channel='telegram')`, привязывает к Client через telegram_id / username / phone.
- WebSocket push через `apps.realtime.utils.push_chat_message` — UI чата на дашборде обновляется в реальном времени.

## Lead bot — `apps/telegram/leads_bot.py`

Отдельный bot-account мониторит канал `TELEGRAM_LEADS_CHANNEL_ID` (например, канал лендинга — туда боты лендинга шлют заявки клиентов).

### Polling вместо webhook

**Telegram webhook на наших серверах не работает** (split-tunnel VPN заворачивает ответный SYN-ACK от Telegram в туннель → у Telegram timeout). Поэтому используем **polling**:

- `apps/telegram/tasks.py:poll_telegram_leads` — Celery task с расписанием каждые 10с через django-celery-beat.
- `getUpdates` с **long-polling timeout=20с** — Telegram сам подвешивает запрос пока не появятся обновления.
- **SETNX-лок** `cache.add('telegram_leads:poll_lock', '1', timeout=POLL_TIMEOUT+10)` — иначе соседние beat-тики ловят `409 Conflict` от Telegram (один long-poll на бот).
- `time_limit = POLL_TIMEOUT + 15` секунд на task.
- Кэш-ключ `telegram_leads:update_offset` хранит offset между прогонами.

### Отключение polling без правки кода

```python
from django_celery_beat.models import PeriodicTask
PeriodicTask.objects.filter(name='poll-telegram-leads').update(enabled=False)
```
django-celery-beat хранит расписание в БД, beat подхватит за пару секунд.

### Парс лида

`_parse_lead(text)` извлекает структурированные данные из сообщения канала (ФИО, телефон, ответы на вопросы анкеты). Поддерживает формат с парами `«Вопрос: Ответ»`.

`create_lead_from_parsed(data)`:
- Дедуп по ClientPhone (`find_client_by_phone`).
- Создаёт `Client(status='lead')`.
- Распределение через `apps/crm/lead_routing.py:route_new_lead("Telegram", ...)` — на сотрудников с `Employee.accept_telegram_leads=True`, fallback — Власов Евгений.
- Личный статус «Лиды из Telegram» в их «Мой канбан» (автосоздаётся `ServiceEmployeeStatus`).
- Логирует через `client_log.record_legacy(event_type='lead_received')` от имени системного «Бот Сириус» (см. `_system_bot_employee()`).

## Webhook leads_bot (резерв)

`apps/telegram/leads_bot.py:leads_webhook(request, secret)` — POST-эндпоинт на случай, если split-tunnel-проблема будет решена и можно будет переключить с polling на webhook. URL `/webhook/telegram-leads/[<secret>/]`, env `TELEGRAM_LEADS_WEBHOOK_SECRET`. Сейчас **не используется**.

## Env-vars

```
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=                           # для первичной авторизации userbot
TELEGRAM_SESSION_STRING=                  # сохранённая Telethon-сессия
TELEGRAM_BOT_TOKEN=                       # bot для лидов (отдельный аккаунт)
TELEGRAM_WEBHOOK_URL=                     # резерв, не используется
TELEGRAM_LEADS_CHANNEL_ID=                # ID канала с лидами лендинга
TELEGRAM_LEADS_WEBHOOK_SECRET=            # если когда-нибудь перейдём на webhook
```

## Management-команды

- `python manage.py run_userbot` — запуск Telethon-userbot в foreground (для отладки).
- `python manage.py import_telegram_history` — импорт исторических чатов клиента через userbot (используется при первичной миграции клиента).
- `python manage.py setup_telegram_leads_webhook` — устанавливает webhook URL у лид-бота (на случай переключения с polling).

## Связь с другими модулями

- **`ClientPhone(purpose='telegram')`** — алиас номера для TG-канала. `find_client_by_phone(..., purposes=['telegram','primary'])`.
- **`Message.channel='telegram'`** + `Message.telegram_message_id` — идентификатор для дедупа.
- **`Client.telegram_id`** — кэш id чата в Telegram (для исходящих).
- **`Employee.accept_telegram_leads`** — флаг получения TG/WA-лидов через `route_new_lead`.
