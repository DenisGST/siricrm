# WhatsApp интеграция (`apps/whatsapp/` через 1msg.io)

Боевой WhatsApp Business канал — провайдер **1msg.io** (форк chat-api.com, обёртка над Meta Cloud API). Подключён 31 мая 2026 как замена Bubble для коммерческого отдела.

## Архитектура — без своих моделей

`apps/whatsapp/` — тонкий слой над общими `Client/Message/StoredFile`:
- `config.py` — env-обёртка (`INSTANCE_ID`, `API_TOKEN=JWT`, `API_BASE`, `TEST_MODE`, `ALLOWED_PHONES`, `WEBHOOK_SECRET`) + `is_configured()` / `is_phone_allowed()`.
- `sender.py` — HTTP-клиент к 1msg + `download_media` для входящих.
- `tasks.py` — Celery: исходящие `send_whatsapp_message_task(message_id)` (retry×3) + приём входящих `process_incoming_wa_message(message, contacts)` / `process_wa_status(status)`.
- `processing.py` — обработчики входящих/статусов (`handle_incoming_message`/`handle_status_update` + helpers). 🛑 **Вынесено из webhook в Celery** после инцидента 09.06.2026: тяжёлая работа (download_media + S3 + lead-routing) в ASGI-обработчике исчерпывала sync-threadpool daphne и вешала прод. Дедуп по `whatsapp_message_id` — внутри обработчика, повторная доставка/постановка безопасны.
- `views.py` — webhook (**только парсит payload и ставит таски, сразу 200**) + media-прокси `wa_file_proxy`.
- `middleware.py` — `WAFileProxyHeaderStripMiddleware` (см. ниже).
- `urls.py` — `/webhook/whatsapp/[<secret>/]` + `/wa/file/<uuid:file_id>/`.

Общие поля: `Message.channel='whatsapp'`, `Message.whatsapp_message_id` (wamid от Meta), `ClientPhone(purpose='whatsapp')`, `Client.whatsapp_phone` (legacy кэш). UI отправки — кнопка в `templates/crm/partials/telegram_chat_panel.html` рядом с Telegram/MAX.

## 1msg.io API

- URL-формат: `https://api.1msg.io/<INSTANCE_ID>/<method>?token=<JWT>`. JWT кладём целиком в `WHATSAPP_API_TOKEN`, в payload и Bearer не используется.
- **Реально работающие методы отправки:** `sendMessage` (текст), `sendFile` (любое медиа — image/video/audio/voice/document, тип определяется по content-type), `sendLocation`, `sendContact`, `sendTemplate`.
- **Не существуют** (404): `sendPTT`, `sendImage`, `sendVideo`, `sendDocument`, `sendAudio`, `sendMedia`, `sendInteractive`. Поэтому в `sender._MEDIA_TYPE_TO_METHOD` ВСЕ медиа маппятся на `sendFile` (включая `voice` — в 1msg нет отдельного PTT).
- **Аудио без `caption`** — иначе 1msg возвращает `{sent:false, error:'Unexpected key "caption" on param "audio"'}`. Для других типов caption ОК.
- 🛑 **`body`/`caption` НЕ может содержать табы или >4 пробелов подряд** → HTTP 500 `Param text cannot have new-line/tab characters or more than 4 consecutive spaces`. Переносы строк `\n` при этом 1msg **принимает** (проверено). `sender.sanitize_wa_text` чистит табы + 4+ пробелов (перенос сохраняет); параметры sendTemplate схлопывают любой whitespace. 🛑 Эта ошибка **перманентна — НЕ ретраить** (иначе оператор видит красный тост N раз; кейс Олейников Роман 25.06: отступ пробелами перед ссылкой).
- **Сервисные методы:** `GET /status`, `GET /me`, `GET /webhook`, `POST /webhook` (body `{"webhookUrl": ["..."]}` или строкой — заменяет полностью).
- **Ответ sendMessage/sendFile успешный:** `{"sent":true, "id":"<wamid>", ...}`. Ошибка: либо HTTP 4xx, либо HTTP 200 + `{"sent":false, "message":"..."}`. Sender ловит оба варианта.

## Webhook на приём

- URL: `https://siricrm.ru/webhook/whatsapp/` (prod) / `https://crmsiri.ru/webhook/whatsapp/` (dev), опц. с `<secret>` если `WHATSAPP_WEBHOOK_SECRET` задан.
- Прописывается через `POST https://api.1msg.io/<INSTANCE_ID>/webhook?token=<JWT>` body `{"webhookUrl":["..."]}` — **заменяет** массив целиком (мы уронили чужие webhook'и при переключении на prod 31 мая).
- 1msg шлёт **flat-формат**: `{"messages":[{id, from, body, type, fromMe, time, chatId, senderName, caption, quotedMsgId}], "ack":[{id, status: sent|delivered|read|failed, error}], "instanceId"}`. Также поддерживается Meta-style `entry → changes → value → messages/statuses/contacts` (на случай переключения 1msg на passthrough).
- **Входящие медиа:** 1msg даёт прямой URL в `message.body` (`https://1msg-ru.hb.ru-msk.vkcloud-storage.ru/...`). `_extract_media_url_and_name` → `_download_wa_media_to_s3` качает через requests, льёт в S3 (`prefix='whatsapp/incoming'`), создаёт `StoredFile(bubble_id='wamedia_<wamid>')`, привязывает `Message.file`. Best-effort — при недоступности CDN ставится текстовый плейсхолдер «(медиа)», обработка не падает.
- **Дедуп** через `Message.whatsapp_message_id`+`channel='whatsapp'`. Незнакомый номер → автосоздание Client(status='lead') + `route_new_lead("WhatsApp", ...)`.
- Ack-статусы (`sent/delivered/read`) обновляют `Message.is_sent/is_delivered/is_read` + push в WS через `push_message_status`. 🛑 Извлекать flat-ключ **`ack`** (не только Meta-style `statuses`) — иначе delivered/read никогда не проставятся (баг до 05.06.2026). Галочки в чате — `telegram_message.html`. См. память chat-message-status-ticks.

## Исходящие медиа: data:URI base64 в payload `body`

**Текущая реализация (с 31 мая 2026):** в `tasks.py` файл качается из S3, кодируется в `base64`, отправляется в payload `body` как `data:<content-type>;base64,<...>`. 1msg документирует три формата для `body`: URL, base64 data:URI, multipart form. Data:URI обходит сразу всю URL-историю:

- нет HEAD-probe (1msg сразу читает байты из payload);
- нет проблем с кириллицей в URL (в 1msg документации: «Кириллические символы в URL файла не поддерживаются — должна быть процент-кодировка»);
- нет эмпирического ~1 МБ лимита, который ловили при URL-режиме (через payload лимит уже от POST body, ~50 МБ у 1msg);
- не нужен публичный домен для CRM (Celery в локалке без публичного URL тоже может слать).

```python
import base64
from apps.files.s3_utils import download_file_from_s3
data = download_file_from_s3(msg.file.bucket, msg.file.key)
ctype = msg.file.content_type or "application/octet-stream"
file_url = f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"
```

В sender ничего менять не пришлось — `body` принимает что угодно (URL или data:URI). `filename` payload — отдельный параметр, в нём кириллица допустима (1msg/Meta её принимают).

## Прокси `/wa/file/<uuid:file_id>/` (резерв)

Изначально (до перехода на data:URI) исходящие медиа шли через прокси-эндпоинт `apps.whatsapp.views.wa_file_proxy` — он стримил `StoredFile` из S3 через наш домен (Beget pre-signed URL валился на HEAD-probe 1msg). Сейчас прокси **не используется**, но оставлен в коде:

- `apps/whatsapp/views.py:wa_file_proxy` — GET+HEAD, стримит файл с корректным `Content-Type` + `Content-Length`. Защита — файл должен быть привязан к `Message(channel='whatsapp', created_at >= now-24h)`.
- `apps/whatsapp/middleware.py:WAFileProxyHeaderStripMiddleware` (первой в `MIDDLEWARE`) — стирает `Vary/Cookie/X-Frame-Options/HSTS/Content-Disposition/Cache-Control/Referrer-Policy/Cross-Origin-*` только для путей `/wa/file/` (WhatsApp Cloud отвергал любой ответ с этими headers).
- `settings.PUBLIC_BASE_URL` (env: `https://siricrm.ru` для prod, `https://crmsiri.ru` для dev/default) — для построения абсолютного URL из task без request.

Если в будущем потребуется отдавать медиа по ссылке (например, для предпросмотра в e-mail, для веб-просмотра клиентом) — инфраструктура готова.

## Кириллица

- **URL файла** в `body` (при URL-режиме) — 1msg не поддерживает, должна быть процент-кодировка. С переходом на data:URI это не актуально.
- **`filename`** в payload — кириллица проходит и в 1msg, и в Meta. Имя файла на телефоне получателя будет читаемым.
- **HTTP `Content-Disposition` нашего прокси** (если возвращаемся к URL-режиму) — Django при кириллице автоматически кодирует через RFC 2047 `=?utf-8?b?...?=` (формат для email!), 1msg валит upload. В коде прокси Content-Disposition полностью **снят** middleware'ом — проблема устранена, но при возврате к URL-режиму помнить.

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

## WABA-шаблоны (sendTemplate) — отправка вне 24-часового окна

Вне 24ч-окна обслуживания (клиент не писал >24ч) Meta блокирует free-form: ack
приходит `status=failed`, `error="This message was not delivered to maintain
healthy ecosystem engagement."`. Отправлять можно **только approved-шаблоны**.

- **Namespace** WABA общий для инстанса: `config.NAMESPACE`
  (`991ceaad_9bf3_4128_b815_54d706ed24a4`, env `WHATSAPP_NAMESPACE`).
- **sender.py:** `send_whatsapp_template(phone, template_name, body_params, language_code)`
  (1msg `POST /sendTemplate`: `{phone, namespace, template:<name>, language:{policy:deterministic,code}, params:[{type:body,parameters:[{type:text,text}]}]}`);
  `create_whatsapp_template(name, body_text, category, language, body_example)` (`POST /addTemplate` — на модерацию Meta); `list_whatsapp_templates()` (синк статусов).
  🛑 `sendTemplate` принимает **имя** шаблона (`template`), не Meta-id.
- **Модель `MessageTemplate`** (apps/crm): `whatsapp_template_name` (латинское имя в Meta),
  `whatsapp_meta_status` (draft/pending/approved/rejected), `whatsapp_category`,
  `whatsapp_params_schema`. `Message.message_template` + `Message.template_params` —
  привязка отправленного шаблона. Таск `send_whatsapp_template_task` (шлёт только при `status=approved`).
- **UI справочников** (`/`→Справочники→Шаблоны): кнопка «↗ В Meta» (`reference_message_template_submit_wa`) → addTemplate, «⟳ Синк WA-статусов» (`reference_message_templates_sync_wa`) → подтянуть результат модерации.
- **UI чата:** кнопка «📋 Шаблон» рядом с WhatsApp → `whatsapp_template_picker` (модалка с approved-шаблонами + поля переменных, имя клиента в {{1}} автоподставляется) → `whatsapp_send_template`. Плюс **авто-подсказка**: при ack `failed` с «ecosystem engagement» под пузырём появляется кнопка «Окно 24ч закрыто — отправить шаблон» (JS `suggestWaTemplate`).
- Базовые 4 шаблона: `first_contact_intro`, `reactivation_no_reply` (MARKETING), `payment_reminder`, `document_request` (UTILITY).

## Выбор номера для исходящего + резолв (баг-фиксы 11.06.2026)

- **WA-номер исходящего** (`tasks._client_whatsapp_phone`) теперь берётся в порядке:
  (1) номер последнего входящего WA (`_last_inbound_wa_phone` — `chatId/author` из `raw_payload`),
  (2) ClientPhone purpose `whatsapp`, (3) `primary`, (4) legacy. Причина: у клиента
  бывает несколько номеров (отдельный для TG), а WhatsApp живёт только на том, с
  которого идёт диалог — иначе Meta «Message undeliverable» (кейс Кирилла Мишичева:
  писал с 79648851455, отвечали на primary 79055043411 без WhatsApp).
- **Входящий WA тегает номер отправителя как `whatsapp`** и для существующего клиента
  (`processing._get_or_create_wa_client`) — самоисцеление для будущих диалогов.

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
