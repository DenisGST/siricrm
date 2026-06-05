# MAX (Mail.ru Group мессенджер) интеграция (`apps/maxchat/`)

MAX — отечественный мессенджер от VK (бывшая Mail.ru Group), доступен через **Bot API** `https://platform-api.max.ru`. У нас один бот, через который сотрудники CRM пишут клиентам и получают входящие.

Используется также как **алёрт-канал** для арбитража (см. `arbitr_integration_claude.md` → MAX-уведомления о капче).

## Архитектура — без своих моделей

`apps/maxchat/` — минималистичный слой над общими `Client/Message/StoredFile`:
- `sender.py` — HTTP-клиент к MAX Bot API (отправка текста + медиа).
- `tasks.py` — Celery `send_max_message_task(message_id)` с retry×3.
- `views.py` — webhook на приём входящих. **MAX Bot API НЕ присылает боту статусы доставки/прочтения** → у исходящих MAX-сообщений доступно только «отправлено» (✓), без «доставлено/прочитано». См. [[chat-message-status-ticks]].
- `urls.py` — `/max/webhook/`.
- **Нет своих моделей и миграций**.

Общие поля: `Message.channel='max'`, `Message.max_message_id`, `Client.max_chat_id` (= user_id чата в MAX). UI кнопка — `#btn-send-max` в `templates/crm/partials/telegram_chat_panel.html`.

## MAX Bot API

- Базовый URL: `https://platform-api.max.ru` (хардкод в `MAX_API_BASE_URL` settings и `sender.py`).
- Auth: header `Authorization: <MAX_BOT_TOKEN>` (без `Bearer`!). Токен получается в кабинете MAX-бота.
- Эндпоинты:
  - `POST /uploads?type=<upload_type>` → `{url, token}`. Шаг 1 загрузки медиа — получение pre-signed URL.
  - `PUT <upload_url>` — собственно загрузка байтов файла.
  - `POST /messages?user_id=<chat_id>` body `{"text": "...", "attachments": [{"type": ..., "payload": ...}]}` — отправка.
  - Для video/audio после upload нужно дождаться обработки (`_wait_attachment_ready` — polling с timeout).
- Маппинг message_type → upload type:
  - `image → image`
  - `video → video`
  - `audio → audio` (включая voice)
  - `document → file`

## Отправка из CRM

1. UI: кнопка «MAX» в форме чата → POST `clients/<uuid>/max/send/` → `apps/crm/views.max_send_message`.
2. View создаёт `Message(channel='max', direction='outgoing', is_sent=False)`, при наличии файла привязывает `StoredFile`.
3. `send_max_message_task.delay(msg.id)` → Celery.
4. Task:
   - Качает файл из S3 (`download_file_from_s3`) если есть.
   - `send_max_message(token, chat_id, text, file_bytes, filename, message_type)`:
     - Если медиа: `_upload_file_to_max` → PUT → `_wait_attachment_ready` → собирает `attachments`.
     - POST `/messages` с `params={user_id: chat_id}`.
   - Возвращает `(ok, max_message_id, err)`.
   - При ok: `Message.is_sent=True, sent_at, max_message_id` + WS push через `push_message_status`.
   - При err: WS toast сотруднику, retry×3.

## Приём входящих

- Webhook URL: `/max/webhook/` (через `MAX_WEBHOOK_SECRET` если задан в env — пока опционально).
- View `apps/maxchat/views.max_webhook`:
  - Парсит payload MAX (формат отличается от Meta/1msg — см. сам код).
  - Находит/создаёт `Client` по `chat_id`.
  - Скачивает медиа в S3.
  - Создаёт `Message(channel='max', direction='incoming')`.
  - Push в WS через `apps.realtime.utils.push_chat_message`.
  - Лог в `client_log` через `log_messenger_message`.
- `_determine_message_type(filename, content_type)` — определение типа по расширению/MIME.

## Алёрты арбитража через MAX

`apps/arbitr/notifications.py:send_captcha_alert(case)` использует `apps.maxchat.sender.send_max_message` для отправки админу при капче на kad.arbitr.ru. Получатель — env `ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID` (пока один на всех — chat_id админа; задел — разнести на `Employee.max_chat_id`).

## Env-vars

```
MAX_BOT_TOKEN=                            # токен MAX-бота
MAX_WEBHOOK_SECRET=                       # опц. — если хотим валидировать webhook
ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID=        # куда слать алёрты арбитра
```

`MAX_API_BASE_URL` — хардкод `https://platform-api.max.ru` в settings (`base.py:236`) и sender.py — менять только если MAX сменит домен API.

## Связь с другими модулями

- **`Client.max_chat_id`** (CharField max_length=64) — user_id чата клиента в MAX. Без него отправка вернёт «client has no max_chat_id» (warning в логах).
- **`Message.channel='max'`** + `Message.max_message_id`.
- **MessageTemplate.channels** содержит `'max'` — модели для шаблонов есть, но без отдельного approval-flow (как у WhatsApp).
