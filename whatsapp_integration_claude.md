# WhatsApp интеграция (`apps/whatsapp/` через 1msg.io)

Боевой WhatsApp Business канал — провайдер **1msg.io** (форк chat-api.com, обёртка над Meta Cloud API). Подключён 31 мая 2026 как замена Bubble для коммерческого отдела.

## Архитектура — без своих моделей

`apps/whatsapp/` — тонкий слой над общими `Client/Message/StoredFile`:
- `config.py` — env-обёртка (`INSTANCE_ID`, `API_TOKEN=JWT`, `API_BASE`, `TEST_MODE`, `ALLOWED_PHONES`, `WEBHOOK_SECRET`) + `is_configured()` / `is_phone_allowed()`.
- `sender.py` — HTTP-клиент к 1msg + `download_media` для входящих.
- `tasks.py` — Celery `send_whatsapp_message_task(message_id)` с retry×3.
- `views.py` — webhook + media-прокси `wa_file_proxy`.
- `middleware.py` — `WAFileProxyHeaderStripMiddleware` (см. ниже).
- `urls.py` — `/webhook/whatsapp/[<secret>/]` + `/wa/file/<uuid:file_id>/`.

Общие поля: `Message.channel='whatsapp'`, `Message.whatsapp_message_id` (wamid от Meta), `ClientPhone(purpose='whatsapp')`, `Client.whatsapp_phone` (legacy кэш). UI отправки — кнопка в `templates/crm/partials/telegram_chat_panel.html` рядом с Telegram/MAX.

## 1msg.io API

- URL-формат: `https://api.1msg.io/<INSTANCE_ID>/<method>?token=<JWT>`. JWT кладём целиком в `WHATSAPP_API_TOKEN`, в payload и Bearer не используется.
- **Реально работающие методы отправки:** `sendMessage` (текст), `sendFile` (любое медиа — image/video/audio/voice/document, тип определяется по content-type), `sendLocation`, `sendContact`, `sendTemplate`.
- **Не существуют** (404): `sendPTT`, `sendImage`, `sendVideo`, `sendDocument`, `sendAudio`, `sendMedia`, `sendInteractive`. Поэтому в `sender._MEDIA_TYPE_TO_METHOD` ВСЕ медиа маппятся на `sendFile` (включая `voice` — в 1msg нет отдельного PTT).
- **Аудио без `caption`** — иначе 1msg возвращает `{sent:false, error:'Unexpected key "caption" on param "audio"'}`. Для других типов caption ОК.
- **Сервисные методы:** `GET /status`, `GET /me`, `GET /webhook`, `POST /webhook` (body `{"webhookUrl": ["..."]}` или строкой — заменяет полностью).
- **Ответ sendMessage/sendFile успешный:** `{"sent":true, "id":"<wamid>", ...}`. Ошибка: либо HTTP 4xx, либо HTTP 200 + `{"sent":false, "message":"..."}`. Sender ловит оба варианта.

## Webhook на приём

- URL: `https://siricrm.ru/webhook/whatsapp/` (prod) / `https://crmsiri.ru/webhook/whatsapp/` (dev), опц. с `<secret>` если `WHATSAPP_WEBHOOK_SECRET` задан.
- Прописывается через `POST https://api.1msg.io/<INSTANCE_ID>/webhook?token=<JWT>` body `{"webhookUrl":["..."]}` — **заменяет** массив целиком (мы уронили чужие webhook'и при переключении на prod 31 мая).
- 1msg шлёт **flat-формат**: `{"messages":[{id, from, body, type, fromMe, time, chatId, senderName, caption, quotedMsgId}], "ack":[{id, status: sent|delivered|read|failed, error}], "instanceId"}`. Также поддерживается Meta-style `entry → changes → value → messages/statuses/contacts` (на случай переключения 1msg на passthrough).
- **Входящие медиа:** 1msg даёт прямой URL в `message.body` (`https://1msg-ru.hb.ru-msk.vkcloud-storage.ru/...`). `_extract_media_url_and_name` → `_download_wa_media_to_s3` качает через requests, льёт в S3 (`prefix='whatsapp/incoming'`), создаёт `StoredFile(bubble_id='wamedia_<wamid>')`, привязывает `Message.file`. Best-effort — при недоступности CDN ставится текстовый плейсхолдер «(медиа)», обработка не падает.
- **Дедуп** через `Message.whatsapp_message_id`+`channel='whatsapp'`. Незнакомый номер → автосоздание Client(status='lead') + `route_new_lead("WhatsApp", ...)`.
- Ack-статусы (`sent/delivered/read`) обновляют `Message.is_sent/is_delivered/is_read` + push в WS через `push_message_status`.

## Исходящие медиа: прокси `/wa/file/<uuid:file_id>/`

**Проблема:** 1msg перед скачиванием делает HEAD-probe. Beget S3 pre-signed URL отвечает **403 на HEAD** (sigv4 подписывает только GET) → 1msg отдаёт `ack: failed, error="Media upload error"`. Пробовать публичный bucket нельзя — медиа клиентов.

**Решение:** прокси-эндпоинт `apps.whatsapp.views.wa_file_proxy` (GET+HEAD) — стримит `StoredFile` из S3 через свой домен с корректным `Content-Type` + `Content-Length`. Защита — файл должен быть привязан к `Message(channel='whatsapp', created_at >= now-24h)`; после WA Cloud его уже зеркалит и наш URL не нужен. URL формируется в `tasks.py` как `f"{settings.PUBLIC_BASE_URL}/wa/file/{msg.file.id}/"`.

**`PUBLIC_BASE_URL` в env** (новая переменная): `https://siricrm.ru` для prod, `https://crmsiri.ru` для dev. Default — `https://crmsiri.ru`. Нужен потому что Celery-task не имеет request.get_host().

## Заголовки прокси: `WAFileProxyHeaderStripMiddleware`

WhatsApp Cloud (через 1msg) отвергает media-upload, если в HTTP-ответе есть `Vary/Cookie/X-Frame-Options/HSTS/Content-Disposition/Cache-Control/Referrer-Policy/Cross-Origin-*`. Тест подтвердил: те же файлы по «голым» публичным URL (Adobe sample, picsum) проходят, наш Django-прокси — нет, пока эти заголовки не убрать.

`apps/whatsapp/middleware.py:WAFileProxyHeaderStripMiddleware` стоит **первой в `MIDDLEWARE`** (её `process_response` отрабатывает последним — после Django security/session/csrf, у которых уже не получится вернуть свои заголовки) и стирает указанный список **только для путей `/wa/file/`**. Остальной сайт сохраняет полную security-обвязку.

> ⚠ Если Django добавит новый security-заголовок в будущем — нужно дополнить список в `_STRIP`. Симптом: после релиза Django/middleware медиа снова перестают доходить, в логах прокси-ответа лишний заголовок.

## Кириллица в filename

- В **payload sendFile** (`{"filename": "Ответ МРЭО.pdf"}`) — кириллица проходит, 1msg/Meta её принимают.
- В **HTTP-заголовке `Content-Disposition`** — Django при кириллице автоматически кодирует через RFC 2047 `=?utf-8?b?...?=` (формат для email!), 1msg валит upload. На нашем прокси Content-Disposition полностью **снят** middleware'ом — проблема устранена.

## Известный лимит на размер медиа

Эмпирически: файлы **<1 МБ** проходят, **>1 МБ** валятся `ack: failed, error="Media upload error"` даже с идеально чистыми headers. Природа лимита неясна — может быть тариф 1msg, может Meta media-by-URL ограничение. WhatsApp Cloud официально разрешает Document до 100 MB, но через `body=<url>` 1msg пилит раньше.

**На сегодня (31 мая 2026):** оставлено как есть — для крупных PDF/изображений по требованию разбираться (1msg support или авто-сжатие). Текст + мелкие медиа работают.

## TEST_MODE и allow-list

- `WHATSAPP_TEST_MODE=true` — `is_phone_allowed()` отдаёт `True` только для номеров из `WHATSAPP_ALLOWED_PHONES` (CSV в E.164 без «+»). Webhook на чужие номера → лог + 200 без записи; исходящие на чужие → sender возвращает `(False, None, 'test_mode_skip')`, task не делает retry.
- На **prod** включаем `TEST_MODE=false` — работают любые номера.
- На **dev** держим `TEST_MODE=true` с allow-list тестовых номеров — чтобы случайно не написать клиенту с разработки.

## Env-vars (`.env.dev` / `.env.prod`)

```
WHATSAPP_INSTANCE_ID=305250
WHATSAPP_API_TOKEN=<JWT eyJ...>         # JWT целиком, не Bearer
WHATSAPP_API_BASE=https://api.1msg.io
WHATSAPP_PHONE=                          # информационно, не используется в коде
WHATSAPP_WEBHOOK_SECRET=                 # опц. — добавляет <secret> в URL
WHATSAPP_TEST_MODE=true|false
WHATSAPP_ALLOWED_PHONES=79610730606,79876581647
PUBLIC_BASE_URL=https://siricrm.ru       # prod; dev — https://crmsiri.ru
```

## UI кнопка отправки

В `templates/crm/partials/telegram_chat_panel.html` кнопка `#btn-send-whatsapp` рядом с Telegram/MAX. JS-обработчик `htmx:afterRequest` должен проверять `btn.id === "btn-send-whatsapp"` наравне с TG/MAX — иначе после отправки **форма не очищается** и ответный partial не вставляется в ленту (пользователь видит «отправлено», но текст остаётся в textarea).

## Сервисные curl-команды

```bash
# Статус инстанса
curl -sS "https://api.1msg.io/305250/status?token=$JWT"
# Что прислал webhook + ack-статусы за последние 5 мин (на prod)
docker logs siricrm-web-1 --since 5m 2>&1 | grep -iE '"ack":\['
# Текущий webhookUrl
curl -sS "https://api.1msg.io/305250/webhook?token=$JWT"
# Поменять webhookUrl (массив URL, заменяет полностью!)
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"webhookUrl":["https://siricrm.ru/webhook/whatsapp/"]}' \
  "https://api.1msg.io/305250/webhook?token=$JWT"
# Smoke-отправка текста (внимание: реально уйдёт на телефон!)
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"phone":"79610730606","body":"smoke"}' \
  "https://api.1msg.io/305250/sendMessage?token=$JWT"
```
