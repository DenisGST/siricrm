# CLAUDE.md — SiriCRM

Этот файл автоматически загружается в контекст Claude Code в каждой сессии.
Держи его компактным и актуальным. Технические детали — в `docs/`, пользовательские инструкции — в `guides/`.

## Что за проект

CRM для юридической фирмы (банкротство физлиц / БФЛ). Django 5.2 + HTMX 1.9.8 + daisyUI 4 (Tailwind, pre-compiled) + Celery + Channels (WebSocket). Интеграции: Telegram (userbot на Telethon + бот), MaxChat, Beget S3 (медиа + бэкапы), DaData.

## Карта окружений

| Окружение | Сервер | Домен | nginx | compose-файл | env-файл |
| --------- | ------ | ----- | ----- | ------------ | -------- |
| **prod**  | 45.90.35.187 | siricrm.ru (+ www, flower., redis.) | системный (не Docker): SSL certbot, антисканеры | `docker-compose.prod-host.yml` | `.env.prod` |
| **dev**   | 5.35.94.218  | crmsiri.ru (+ www) | докеризованный (в стеке), антисканеры | `docker-compose.prod.yml` | `.env.dev` |

Разработка сейчас ведётся на **dev** (5.35.94.218). Prod — боевой, не трогать без необходимости.

**Путь к репозиторию различается:** dev — `/var/www/siricrm`, prod — `/var/www/projects/siricrm` (и `HOST_REPO_DIR` в `.env.*` должен указывать на этот путь — нужен для `rebuild`). **SSH dev→prod НАСТРОЕН** — `ssh siri-prod` (root, см. `~/.ssh/config`; dev — `ssh siri-dev`). Удобно для прямой диагностики прода (логи, `docker ps`, перезапуски).

### Запуск стека
```bash
# dev:
ENV_FILE=.env.dev  docker compose -f docker-compose.prod.yml      --env-file .env.dev  up -d
# prod:
ENV_FILE=.env.prod docker compose -f docker-compose.prod-host.yml --env-file .env.prod up -d
```
Контейнеры: `db redis web(daphne) celery celery-beat userbot nginx certbot backup devops-runner` (на prod ещё `flower redis-commander`; на dev nginx+certbot докеризованы).

**Важно:** `docker compose restart <svc>` НЕ перечитывает `env_file` — для смены env нужен `up -d --force-recreate <svc>`. После пересоздания `web` на dev часто нужен `restart nginx` (upstream IP меняется).

### Settings
`config/settings/` — пакет: `base.py` + `dev.py` + `prod.py`. Переключение через `DJANGO_ENV` (нет переменной → dev). **Оба сервера используют `prod.py`** (в `.env.dev` тоже `DJANGO_ENV=prod`) — dev отличается только содержимым env-файла. Секреты только через `.env*` — **никогда не коммитить** `.env.prod` / `.env.dev` (они в `.gitignore`, шаблоны — `.env.{prod,dev}.example`).

`ALLOWED_HOSTS` в `prod.py` приходит из env + дополнительно хардкодом `45.90.35.187` и `5.35.94.218` (внутренние запросы серверов к самим себе по IP). Sentry-фильтр `before_send` дропает `DisallowedHost`-инциденты от внешних сканеров.

**Сессии — в Redis** (`SESSION_ENGINE = "django.contrib.sessions.backends.cache"`), а не в БД. Так пользователя не выкидывает на логин во время `pull_db`/`push_db`, которые дропают public schema (и заодно `django_session`, если бы она там была).

### Миграции / статика
`web` при старте сам делает `collectstatic --noinput && migrate --noinput`. Storage — `whitenoise.CompressedManifestStaticFilesStorage` (строгий: ссылка на отсутствующий static → ошибка).

## Структура

```
apps/core           — сотрудники, отделы, дашборд-конфиг, health endpoint (/health/), EmployeeLog (вход/выход/действия), мониторинг+алёрты (tasks.py)
apps/crm            — клиенты, услуги, канбаны, лог событий/действий (ClientLogEntry), API
apps/files          — файловый менеджер клиента (папки/дерево/превью), S3
apps/realtime       — WebSocket consumers (Telegram-чат, уведомления), channels
apps/telegram       — userbot (Telethon), бот, авторизация по TG
apps/maxchat        — интеграция MaxChat
apps/whatsapp       — интеграция WhatsApp через 1msg.io (приём + отправка + прокси медиа)
apps/consultations  — график консультаций
apps/questionnaire  — анкеты БФЛ (типизированные вопросы), PDF через ReportLab, S3
apps/devops         — DevOps-панель (см. ниже)
apps/arbitr         — мониторинг арбитражных дел kad.arbitr.ru (Selenium-парсер, см. ниже)
apps/finance        — финансовый учёт: Payment, Charge, справочники, генератор графика платежей
apps/afd            — автоформирование документов (договор БФЛ, заявление о банкротстве)
apps/scans          — «Входящие сканы»: приём с офисного МФУ через локальный агент (см. ниже)
config/             — settings/, urls, asgi (ASGI: HTTP+WS через daphne), celery
templates/          — Django-шаблоны (проект НЕ использует base.html — dashboard.html самодостаточен)
docs/               — технические доки (deployment, migration, legacy quickstart)
guides/             — пользовательские инструкции (devops-panel.md и т.д.)
```

## DevOps-панель (apps/devops)

- UI на dev: `https://crmsiri.ru/devops/` (только `is_superuser`). Дашборд разбит по секциям: Состояние серверов · Базы данных · Деплой · S3 · История. Опасные действия — модалки подтверждения (ввод кодового слова; `dev→prod` ещё и чекбокс).
- HTTP-агент: `https://<env>/devops/agent/...` — Bearer-токен из env `DEVOPS_AGENT_TOKEN` целевого сервера (на dev в `.env.dev` есть `DEVOPS_AGENT_TOKEN_PROD` — токен прода; `Environment.agent_token_env` указывает, какую переменную брать). Окружения в БД: `dev` (этот сервер, был `self`) и `prod` — оба активны.
- Контейнер `devops-runner` — Celery worker (очередь `devops`), доступ к `docker.sock` + git/docker CLI + compose plugin; монтирует репо по `HOST_REPO_DIR`
- Handlers (см. `apps/devops/handlers/`): `status` (контейнеры + **CPU/RAM** через docker stats + /proc/meminfo, диск, миграции, git), `daily_stats` (статистика прода за сутки: сообщения/документы/рабочее время — для Telegram-кнопки, см. «Мониторинг»), `backup` (pg_dump→gzip→S3+локально, отдаёт pre-signed download URL), `list_backups`, `s3_stats` (статистика бакетов по префиксам), `disk_usage` (df / + размеры репо-каталогов + Docker images/volumes/cache через docker.sock), `git_log` (последние коммиты — для выбора точки отката), `deploy` (git pull --ff-only + migrate + restart web/celery/celery-beat/**userbot**), `rollback` (git reset --hard на коммит + попытка авто-реверса миграций; отказ на грязном дереве), `rebuild` (git pull + docker compose build + up -d), `pull_db` (защитный бэкап dev → backup на источнике → restore на dev), `push_db` (бэкап dev → шлём цели `restore_db`), `restore_db` (на цели: защитный бэкап себя → скачать дамп → drop schema + restore), `dumpdata_tables`/`loaddata_tables` (выборочный sync по Django-моделям через JSON-фикстуры, UPSERT по pk — для справочников), `pull_tables`/`push_tables` (LOCAL-оркестраторы выборочного sync; на dev собирают dumpdata→S3→loaddata)
- **`pull_db`/`restore_db` переживают свой же drop schema**: до restore снапшотят `DevopsAction`+`DevopsAgentJob` (свою же tracking-запись) и Environment-записи; после restore возвращают через `update_or_create` + дёргают `manage.py devops_setup`. Иначе action висел running в UI вечно, а Environments на dev исчезали (если на источнике не настроены).
- Поток: dev-панель создаёт `DevopsAction` → либо локальный `DevopsAgentJob` в dev-runner (`pull_db`/`push_db`), либо HTTP на агента цели → его `DevopsAgentJob` в его runner. Для env `dev` HTTP идёт петлёй на `crmsiri.ru`.
- Опасные действия (`pull_db`, `push_db`, `deploy`, `rebuild`, `rollback`) требуют подтверждения в UI
- Синк статуса `DevopsAction`: HTMX-поллинг раз в 2с при открытой карточке + фоновый Celery-task `devops.sync_action` в dev-runner (общая логика — `sync_action_once` в `tasks.py`). Таск перепланирует сам себя `apply_async(countdown=3)` до done/failed, потолок 60 мин. Action закрывается даже если вкладка закрыта. Транзитивные DB-ошибки (БД схемы временно нет во время `pull_db`) — перепланируются, цепочка не обрывается.
- `action_poll` редиректит на `action_detail` при не-HTMX запросе — чтобы после логин-редиректа пользователь не приземлялся на голый партиал.
- `deploy`/`rebuild`/`rollback` НЕ перезапускают сам `devops-runner` — после деплоя нового кода на сервер его воркер остаётся на старом коде, нужен ручной `docker compose ... restart devops-runner` на этом сервере (иначе новые action_type / новые celery-tasks типа `sync_action` падают с «Неизвестный action_type» или просто не запускаются)
- **Agent API напрямую (без UI):** с dev можно дёрнуть prod-агента через curl, токен — `DEVOPS_AGENT_TOKEN_PROD` из `.env.dev`. POST `https://siricrm.ru/devops/agent/jobs/` с `Authorization: Bearer $TOKEN` и телом `{"action_type":"deploy","params":{"branch":"feat/production-ready"}}` → ответ содержит `id` (НЕ `job_id`!) → опрос статуса GET `/devops/agent/jobs/<id>/` пока `status in {done, failed}`. Удобно для скриптовых деплоев и когда UI не открыт. Все handlers те же что в UI (`status`, `deploy`, `rebuild`, `backup`, и т.д.).

## Арбитраж — мониторинг kad.arbitr.ru (apps/arbitr)

**Полное описание:** [`arbitr_integration_claude.md`](./arbitr_integration_claude.md) — антидетект-стек Selenium+Xvfb, прогрев сессии, селекторы, capture detection, скачивание PDF, IP-репутация, отладка через `kad_probe`, deploy на prod.

Кратко: Selenium+headed Chrome через Xvfb обходит anti-bot kad. Отдельный контейнер `arbitr-runner` (celery queue `arbitr`), работает 18:00–08:00 МСК. SEARCHING → MONITORING через UI «Это моё дело» в `/arbitr/`. Алёрты о капче в MAX. Полная документация по селекторам и ловушкам — в файле выше.
## UI/UX: auth + idle UX

Multi-tab logout (grace-версия, **без beacon на unload**); idle-таймаут 10 мин с warning/locked-overlay (без редиректа на /login/); keepalive активности через `GET /api/session/idle-check/?a=1`. **🛑 Инварианты keepalive хрупкие — НЕ ломать** (легко вернуть массовые ложные логауты). Полное описание + «не ломать»-блок + эндпоинты `/api/session/`: **[`docs/auth-idle-ux.md`](./docs/auth-idle-ux.md)**.
> ⚠ Карточки клиента как отдельной страницы НЕТ (`apps/crm/urls.py`) — открывается `{% url 'chat' client.id %}` HTMX-swap'ом в `#content-area`. Детали — `docs/ui-conventions.md`.

## Инфраструктурные особенности

- **VPN (Amnezia/WireGuard)** на обоих серверах — split-tunnel: через VPN идёт только трафик к Telegram-подсетям (`149.154.160.0/20`, `91.108.x.x/22`) и Anthropic (`160.79.104.10/32`, `34.36.57.103/32`), остальное напрямую. **Не ставить `AllowedIPs=0.0.0.0/0`** — порвётся SSH.
  - prod: обычный WG, интерфейс `claude0`, `/etc/wireguard/claude0.conf`, peer `72.56.73.137:37539`
  - dev: AmneziaWG (обфускация), интерфейс `awg0`, `/etc/amnezia/amneziawg/awg0.conf`, peer `72.56.73.137:33886`, systemd `awg-quick@awg0`
- **Telegram webhook'и не работают на наших серверах** — split-tunnel заворачивает ответный SYN-ACK на входящие от Telegram обратно в туннель, Telegram видит `Connection timed out`. Для приёма обновлений используем **polling** (`getUpdates`) через Celery beat — `apps/telegram/tasks.py:poll_telegram_leads` каждые 10с с long-poll timeout=20с и SETNX-локом `cache.add('telegram_leads:poll_lock')` (иначе параллельные таски ловят 409 Conflict). Чтобы выключить polling, не трогая код — `PeriodicTask.objects.filter(name='poll-telegram-leads').update(enabled=False)` (django-celery-beat хранит расписание в БД, beat подхватит за пару секунд).
- **S3 (Beget Cloud)**, endpoint `https://s3.ru1.storage.beget.cloud`, region `ru1`. Бакеты: prod media `1464bbae4a12-sirius-s3`, prod backups `1464bbae4a12-backup` (отдельные ключи `AWS_BACKUP_*`), dev media+backups `1464bbae4a12-siridev-s3`. **Beget валится на boto3 PUT (`XAmzContentSHA256Mismatch`)** — загрузка/скачивание только через pre-signed URL + `requests`.
- **Telegram userbot на dev** пока спит (нет credentials) — `userbot.py` gracefully выходит при пустых `TELEGRAM_PHONE`/`TELEGRAM_SESSION_STRING`.
- **userbot держит Python-код в памяти** → при деплое с миграциями (например, рефактор моделей: `ClientEvent`→`ClientLogEntry`, дроп таблицы) контейнер на старом коде падает на несуществующих таблицах. Поэтому `siricrm-userbot-1` добавлен в `RESTART_CONTAINERS` deploy-handler'а (`apps/devops/handlers/deploy.py`) — деплой его рестартует вместе с web/celery. Рестарт безопасен: Telethon переподключается по `TELEGRAM_SESSION_STRING`. (Аналогичная ловушка была у userbot 01.06.2026 после деплоя СОБЫТИЙКИ.)

## Лиды / Телефоны / Маршрутизация

`crm.ClientPhone` — единый источник телефонов клиента; **искать через `find_client_by_phone`** (утилиты `apps/crm/phone_utils.py`, `Client.phone`/`whatsapp_phone` — только кэш). Маршрутизация нового лида — `apps/crm/lead_routing.py:route_new_lead` (на сотрудников с `Employee.accept_telegram_leads`, fallback Власов). Подробно: **[`docs/leads-routing.md`](./docs/leads-routing.md)**.

## Объединение карточек-дублей (кнопка «Объединить»)

Кнопка-иконка 🔀 на карточке клиента (чат-панель + карточки главного канбана) — право **`Employee.can_merge_clients`** (вкл. Власову + суперам; фильтр `{{ user|can_merge_clients }}`, хелпер `apps.core.permissions.can_merge_clients`). Модалка: поиск 2-й карточки → таблица сравнения Клиент1/Клиент2 с выбором «что в итоге» (одиночные поля — К1/К2; коллекции телефоны/услуги/платежи/сообщения/файлы/адреса/лог — К1/К2/Объединить) + выбор выжившей карточки + живой результат. Движок — **`apps/crm/client_merge.py`** (`compare_clients` + `merge_clients`): перенос FK, телефоны как additional (глобальный unique `phone,purpose`), файлы по slug, ClientEmployee unique, защита от потери связей. Вью `client_merge_modal/search/compare/execute`. 🛑 Модалка через `.showModal()` (top-layer, иначе под чатом); дубли услуг НЕ схлопываются. Подробно — память `client-merge-procedure`.

## Поиск (глобальный + фильтр канбана)

**Полное описание:** [`docs/find.md`](./docs/find.md) — глобальный поиск (`global_search`, `global_search_results.html`), клик по клиенту (строка = похожие по ФИО в `#flt-q`; 🎯 «Только этот» = точно по `cid`), кнопки действий с hover-цветами (`--gs-hc`), фильтр канбана (`kanban_column` параметры `q`/`cid`, форма `#kanban-filter-form`, `resetKanbanFilters`), гоччи (svg-цвет на hover, нет иконки `target` → эмодзи 🎯, `form.reset()` не чистит скрытые поля).

Кратко: главный поиск в шапке → `global_search` (`/api/global-search/`). Клик по строке клиента фильтрует канбан по ФИО (`#flt-q`, показывает тёзок); кнопка **🎯 «Только этот»** — точный фильтр по id (`#flt-cid` → `kanban_column?cid=`, одна карточка). `cid` и `q` взаимоисключающие; «Сбросить фильтр» (`resetKanbanFilters`) явно чистит оба.

## WhatsApp интеграция через 1msg.io (apps/whatsapp)

**Полное описание:** [`whatsapp_integration_claude.md`](./whatsapp_integration_claude.md) — 1msg.io API, webhook flow, data:URI base64 для исходящих медиа, прокси `/wa/file/` (резерв), кириллица, TEST_MODE, env-vars, curl-шпаргалка.

Кратко: тонкий слой `apps/whatsapp/` (config+sender+tasks+views+processing, без своих моделей) над общими Client/Message/StoredFile. Боевой канал клиента — JWT-токен 1msg в `WHATSAPP_API_TOKEN`. Все медиа уходят через `sendFile`. **Исходящие медиа** — `data:<ctype>;base64,<...>` в payload (после нескольких итераций URL-режима — прокси, headers, лимиты, кириллица). Прокси `/wa/file/<uuid>/` оставлен в коде на случай URL-режима. UI-кнопка `#btn-send-whatsapp` в `telegram_chat_panel.html` — проверять в `htmx:afterRequest`, иначе форма не очищается после отправки.

🛑 **Приём вебхука — только парсинг + постановка в Celery** (`processing.py:handle_incoming_message`/`handle_status_update`, таски `process_incoming_wa_message`/`process_wa_status`). Тяжёлую работу (скачивание медиа из CDN + S3 + lead-routing) НЕЛЬЗЯ делать в ASGI-обработчике — sync-threadpool daphne исчерпывался и сервер зависал (инцидент 09.06.2026, память `wa-webhook-async-incident`).

**WABA-шаблоны (sendTemplate) — отправка вне 24-часового окна.** Вне окна Meta блокирует free-form (`failed` «healthy ecosystem engagement»), слать можно только approved-шаблоны. `sender.send_whatsapp_template/create_whatsapp_template/list_whatsapp_templates` (1msg `sendTemplate`/`addTemplate`/`templates`; namespace `config.NAMESPACE`). 🛑 `sendTemplate` принимает **имя** шаблона (`MessageTemplate.whatsapp_template_name`), не Meta-id. Модель `MessageTemplate` (`whatsapp_meta_status` draft→pending→approved). 🛑 **Строки `MessageTemplate` per-DB** (Meta-шаблоны общие на инстанс) — заводить на dev И на prod. UI: справочники (создать → «↗ В Meta» → «⟳ Синк»), чат — кнопка «📋 Шаблон». Подробно: `whatsapp_integration_claude.md` + память `wa-templates-feature`.

🛑 **Номер исходящего WA** (`tasks._client_whatsapp_phone`) = номер последнего входящего WA (`chatId`), затем purpose whatsapp/primary — у клиента бывает несколько номеров, WhatsApp живёт только на том, с которого пишет (иначе «Message undeliverable»). Входящий тегает номер отправителя как `whatsapp`.

## Telegram интеграция (apps/telegram)

**Полное описание:** [`telegram_integration_claude.md`](./telegram_integration_claude.md) — userbot (Telethon) + leads-bot, polling vs webhook (split-tunnel ловушка), SETNX-лок, отправка из CRM, env-vars, отключение polling через django-celery-beat.

Кратко: **два TG-канала** — userbot (Telethon, основной аккаунт компании для CRM-чата) и leads-bot (отдельный bot-account, мониторит канал с лидами лендинга). Webhook не работает из-за WireGuard split-tunnel → **polling getUpdates через Celery beat каждые 10с** с SETNX-локом (иначе 409 Conflict). Userbot — отдельный контейнер `userbot`. UI-кнопка `#btn-send-telegram` в чате. Лиды → `route_new_lead("Telegram", ...)` на сотрудников с `Employee.accept_telegram_leads`.

🛑 **Отправка userbot'ом** (`telegram_sender.send_telegram_message`): если получателя нет в кэше сессии → `Could not find the input entity for PeerUser`. Резолв `_resolve_peer`: по id → по `@username` (передаётся из `Client.username`) → через `get_dialogs`. Поэтому при сохранении лида важно класть `username`.

## MAX интеграция (apps/maxchat)

**Полное описание:** [`maxchat_integration_claude.md`](./maxchat_integration_claude.md) — MAX Bot API endpoint'ы, 3-step upload медиа (uploads → PUT → wait_attachment_ready → messages), webhook, использование как алёрт-канала для арбитража.

Кратко: тонкий слой `apps/maxchat/` (sender+tasks+views+processing, без своих моделей) над `Client/Message/StoredFile`. MAX Bot API `https://platform-api.max.ru` — auth header `Authorization: <MAX_BOT_TOKEN>` без Bearer. Отправка медиа: 3 шага (`POST /uploads` → `PUT <url>` → `POST /messages`). `Client.max_chat_id` обязателен. UI-кнопка `#btn-send-max` в чате. Также используется арбитром для алёртов о капче (`ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID`). 🛑 Приём вебхука вынесен в Celery (`processing.py:handle_max_event`, таска `process_incoming_max_event`) — как у WhatsApp, чтобы не вешать daphne. Id отправленного — в `message.body.mid` (не `message.id`).

## Чат: поле ввода + единая модалка шаблонов (`telegram_chat_panel.html`)

- Поле ввода 85% слева, кнопки **Telegram/MAX/WhatsApp** — столбиком справа; ряд действий (Файл/Аудио/Эмодзи/📋Шаблон/статус) прижат влево, `zoom:0.8`.
- **Enter = отправка** в канал по умолчанию; **Shift/Ctrl+Enter — перенос строки**. Канал по умолчанию = последнего входящего (`view: default_channel`), показан **бэйджем на бордюре поля** (`#enter-channel-name`, обновляется на лету по `message-badge-*-in` при WS-входящем). JS кликает соответствующую `#btn-send-*`.
- **Кнопка «📋 Шаблон»** → единая модалка `chat_template_picker.html` (view `whatsapp_template_picker`, открывается `?channel=`): табы каналов (только доступные клиенту) → `select` шаблона → блок настроек. WA — поля переменных `{{N}}` (approved, sendTemplate); TG/MAX — редактируемый текст с подставленными CRM-плейсхолдерами. Единый POST `chat_send_template`. 🛑 Модалка через `.showModal()` без `modal-open` (иначе рисуется ПОД чатом). Авто-подсказка «Окно 24ч закрыто» под отбитым WA-пузырём.

## Статусы сообщений в чате (галочки доставки/прочтения)

Исходящие в чате показывают статус: ✗ не доставлено (красный) · ✓✓ синие — прочитано · ✓✓ серые — доставлено · ✓ — отправлено · ⏳ — отправляется. Поля `Message.is_sent/is_delivered/is_read/is_failed/error_text`. Рендер — `telegram_message.html` (футер пузыря); живое обновление — `push_message_status` → consumer `chat_message_status` → JS в `telegram_chat_panel.html` (**все звенья несут is_delivered/is_read/is_failed, не только is_sent**). По каналам:
- **WhatsApp**: 1msg.io шлёт ack-статусы вебхуком ключом **`ack`** (не `statuses`!) — извлекать оба. `status=failed` (напр. «healthy ecosystem engagement» = блок WhatsApp по 24-часовому окну, не баг) → `is_failed` + красный «✗ не доставлено».
- **Telegram**: «клиент прочитал наше исходящее» = `UpdateReadHistoryOutbox` (НЕ `Inbox`!) — userbot ловит оба. `is_delivered` не ставится (sent→read).
- **MAX**: Bot API прочтения боту не отдаёт → только «отправлено».

См. память `chat-message-status-ticks`.

## Права видимости клиентов (настраиваются в UI)

Логика в `Client.objects.visible_to(user)` (`apps/crm/managers.py`). Видят ВСЕХ клиентов:
- `is_admin` / `is_superuser` / `managing_partner` / `head_dep`;
- `Employee.is_owner=True` (root-флаг для основателя — ставится только суперюзером в карточке сотрудника);
- сотрудник отдела с `Department.sees_all_clients=True` (например, «Отдел продаж»).

Остальные сотрудники видят клиента, если: они в `Client.employees` (ответственный), ИЛИ в `Service.employees` (исполнитель), ИЛИ у клиента есть `Service` с `common_status.department == их отдел` (этап обслуживания закреплён за их отделом через `ServiceCommonStatus.department`).

Helper `apps.core.permissions.can_view_all_clients(user)` + шаблонный фильтр `{% load permissions_tags %}` `{{ user|can_view_all_clients }}` — единая точка проверки «может смотреть всю компанию». Использовано в чат-фильтре «Все» (видна только management + `sees_all_clients`) и в backend-защите `telegram_clients_list` (scope='all' для остальных принудительно режется до 'mine').

Фильтры чат-панели «Мои» / «Отдел» учитывают и `Client.employees`, и `Service.employees` (через `Q(...)|Q(...).distinct()`).

## UI/UX-конвенции

Канбан-колонка (`PAGE_SIZE=12`, intersect-подгрузка, `#id`-root), `#global-progress` (watchdog от «вечной» полосы), `client_edit` без reload, файловый менеджер (office/PDF inline-превью), глобальный поиск, `showToast()`, кэш статики (`STORAGES` обязателен), `{% comment %}` для multiline, чат-модалка (ленивый список, `_activeTelegramClientId`, `?pin_client_id`). Подробно: **[`docs/ui-conventions.md`](./docs/ui-conventions.md)**. Виджеты-счётчики дашборда + 🛑 гочча наследования `hx-target` (затирала `<body>`) — **[`docs/dashboard-widgets.md`](./docs/dashboard-widgets.md)**.

## Лог клиента: события + действия (`ClientLogEntry`) — СОБЫТИЙКА

Единый лог `ClientLogEntry` (kind `event`/`action`, справочники `EventType`/`ActionType`). Писать через `apps/crm/client_log.py` (`record_event`/`record_action`/`record_legacy`), **не** `ClientLogEntry` напрямую. Модалка `client_events_modal` (`GET /clients/<uuid>/events/`, add — `POST .../events/add/`). Подробно (модель, миграции 0070-0078, UI, доработки): **[`docs/crm-log-sobytiyka.md`](./docs/crm-log-sobytiyka.md)**.

## Канбаны: инбокс «Не принято», передача услуги, права на график

Инбокс «Не принято» (`ServiceEmployeeStatus.is_inbox`); передача услуги (`apps/crm/service_transfer.py` — **статус НЕ меняется**, см. память `no-inventing-business-logic`); DnD руководителя на чужом канбане (`viewed_employee`); права графика (`finance/permissions.py:can_edit_schedule`); поиск `#flt-q`; в карточке услуги — кликабельное ФИО (→`client_edit`) + кнопка событийки. Подробно: **[`docs/kanban.md`](./docs/kanban.md)**.

## Входящие сканы — приём бумажной корреспонденции (`apps/scans/`)

**Полное описание:** [`scans_integration_claude.md`](./scans_integration_claude.md) — локальный scan-agent (Bearer-intake, токен/адрес парой), модель `IncomingScan`, UI лотка (фильтры/архив/пакетная привязка/typeahead контрагента/бейдж), привязка к сотруднику по `Employee.scanner_name`, гочча превью octet-stream, трей-агент и лаунчеры автозагрузки.

Кратко: бумага → офисный МФУ → **локальный агент `tools/scan-agent/` пушит** файл `POST /scans/agent/intake/` (Bearer `SCAN_AGENT_TOKEN`, НЕ поллинг) → лоток `/scans/` → привязка к клиенту (папка + опц. `Correspondence`). Доступ — флаг `Employee.can_handle_scans`. Каждый по умолчанию видит свой сканер (`Employee.scanner_name` = `device` агента; в проде `scanner-jur`/`scanner-docs`/`scanner-buh` по отделам), выпадашка — чужие/«Все». **В проде включено.** 🛑 В агенте `device` обязателен; токен и url — из одного окружения (иначе 401).

## Мониторинг доступности + Telegram-бот (`apps/core/tasks.py`)

Кросс-серверно: **dev мониторит прод** (прод мониторит dev — код готов, но не включён). Задача живёт на МОНИТОРЯЩЕМ сервере → алёрт придёт даже при полном падении целевого стека. Подробно — память `health-monitoring`.

- **`monitor_health`** (beat раз в минуту): GET `HEALTH_MONITOR_TARGET_URL`/health/, при N неудачах подряд (`HEALTH_MONITOR_FAIL_THRESHOLD`) → алёрт в MAX + Telegram, при восстановлении — «ОК». Дедуп через Redis. No-op, если `HEALTH_MONITOR_TARGET_URL` пуст.
- **`poll_monitor_bot`** (beat, dev, gate `MONITOR_BOT_POLL=true`): long-poll getUpdates Telegram-бота, inline-кнопки **«🖥 Статус прода»** (→ прод-агент `status`) и **«📊 Статистика за сутки»** (→ прод-агент `daily_stats`). Данные прода берёт по HTTP с прод DevOps-агента (`PROD_AGENT_URL`+`DEVOPS_AGENT_TOKEN_PROD`) → работает даже если прод-web завис.
- **Доступ к боту — только белый список** `MONITOR_BOT_ALLOWED_CHAT_IDS` (деф. = `HEALTH_ALERT_TELEGRAM_CHAT_ID`, сейчас только Каныгин). Fail-closed; чужим — отказ.
- env-vars (на dev): `HEALTH_MONITOR_TARGET_URL/LABEL`, `HEALTH_ALERT_MAX_CHAT_ID` (деф.=арбитровый), `HEALTH_ALERT_TELEGRAM_CHAT_ID`, `MONITOR_BOT_POLL`, `PROD_AGENT_URL`. `TELEGRAM_BOT_TOKEN` теперь в settings (нужен боту).
- 🛑 Алёрты/бот в Telegram идут через бота — **пользователь должен один раз нажать Start** у бота, иначе `chat not found`. MAX так не требует.
- 🛑 Новый агент-handler (`daily_stats`) требует ручного рестарта прод `devops-runner` после деплоя.

## Bubble-импорт (`apps/bubble_import/`)

**Полное описание:** [`bubble_integration_claude.md`](./bubble_integration_claude.md) — лимит cursor-пагинации 50k + окна 30 дней, management-команды (`sync_projectbfl_aliases`, `reapply_failed_wa`, `create_leads_from_failed_wa`, `fetch_bubble_since`, `backfill_status_from_bubble`), live vs `/version-test/`, гочча с `BubbleRecord.target_id` (str vs UUID).

Кратко: перенос данных с bubble.io в SiriCRM через BubbleRecord-буфер (JSONB raw + appliers). Боевой URL `BUBBLE_API_BASE=https://siricrmdev.ru/api/1.1/obj` — НЕ `/version-test/`. Долгие apply'и — через `docker compose exec -d web nohup ...`.

## АФД — автоматическое формирование документов (`apps/afd/`)

Генерация документов с автозаполнением из CRM. Реализовано: **договор юруслуг БФЛ** (плейсхолдеры в .docx-шаблоне) и **заявление о признании банкротом + приложения** (секционный конструктор). Кнопки «Составить договор» и «⚖ Сформировать заявление о банкротстве» в модалке услуги.

### Договор БФЛ

- **Модели:** `ExecutorOrg` (реквизиты Исполнителя — справочник, редактируется в UI; плейсхолдеры `{ispolnitel}`/`{Реквизиты_исполнителя}`/`{Исполнитель}`), `DocumentTemplate` (шаблон .docx в S3, `kind=contract_bfl`), `GeneratedDocument` (история генераций).
- **Движок** (`docx_engine.py:render_docx`) — заполняет `{плейсхолдеры}` **с учётом разбиения слова на runs** (Word дробит текст на `<w:r>`): склеивает run'ы абзаца, заменяет, пишет результат в первый run и очищает остальные. Обходит и таблицы, и колонтитулы.
- **PDF** (`pdf_utils.py:docx_to_pdf`) — `soffice --headless --convert-to pdf` (LibreOffice). **`libreoffice-writer` добавлен в `Dockerfile`** → ⚠ при выкатке на prod нужен **`rebuild`, не `deploy`** (и так из-за новых зависимостей `python-docx`, `pypdf` в requirements). Конвертация синхронная в `web` (не celery), ~3–10с.
- **Приложения к договору** склеиваются в итоговый PDF через `pypdf` (`pdf_utils.merge_pdfs`): договор + график платежей (reportlab, `appendix.schedule_appendix_pdf`) + анкета (переиспользует `questionnaire.pdf.generate_response_pdf`).
- **Контекст/проверка** (`contract_bfl.py`): `check_requisites(service)` → группы полей с галочками (показывается в модалке перед генерацией); `build_context` мапит 36 плейсхолдеров. `{N платеж}` (1..12) — из `Charge` с title «Юруслуги…»; `{сумма_публикации}`=`procedure_costs`, `{summDop}`=`additional_costs`.
- **Результат** (`generator.py:generate_bfl_contract`): итоговый PDF → `service.contract_file` + папка «Договоры» файл-менеджера клиента; редактируемый .docx — туда же; событийка `record_action('contract_created', stored_file=pdf)`.
- **UI:** панель `/afd/` (пункт меню «АФД — документы», `is_references_access`) — CRUD реквизитов Исполнителя и шаблонов + история. Модалка проверки/генерации — `afd:contract_check` → `afd:contract_generate`.
- **Сидинг:** `python manage.py afd_seed` (идемпотентно) — дефолтный (пустой) `ExecutorOrg`, шаблон договора из `apps/afd/seed_templates/contract_bfl.docx` → S3, пункт меню. **Реквизиты Исполнителя сидятся пустыми — заполнить в UI**, иначе договор сгенерится с пустыми {ispolnitel}/реквизитами.

### Заявление о банкротстве (иск) — секционный конструктор

- **Модели:** `IskTemplate` (вариант шаблона) → `IskSection` (раздел: `order, title, body` с плейсхолдерами `{…}`, `block_type`, `align/bold`, `is_optional`, `include_condition` — ключ флага для условных разделов). **Редактируются в панели `/afd/`** (добавить/править/удалить/двигать ↑↓) — это «смысловые разделы».
- **Контекст** (`isk_context.py:build_isk_context(service, overrides, sro, response)`): 🛑 **единый источник кредиторов — `resolve_creditors(at)`** (`at`=answers_by_type анкеты): bank/mfo/marketplace/utility/court/fine/other_debts → резолв `bank_id`/`mfo_id` в `LegalEntity` (реквизиты ОГРН/ИНН/адрес; кто без связи — флагуется). Из него же суммы (total/overdue/%), перечень `{debts_list}`, блок `{creditors_block}`, приложение «Список кредиторов». Плюс narrative `{income/family/property/deals_text}`, УФНС по региону, прежняя фамилия из `Client.name_history`. **Новый табличный тип долга в анкете → добавлять и в `resolve_creditors`.**
- **Движок** (`isk_engine.py:render_isk_docx`) — программная сборка из секций (python-docx): плейсхолдеры + условные разделы. 🛑 Шапка (`block_type` court_header/creditors_header) **смещена вправо на 8 см** по делопроизводству (адресатный блок в правой половине).
- **Приложения-формы** (`isk_appendices.py`, docx-таблицы): «Список кредиторов и должников» (форма Минэк №530), «Опись имущества», «Ходатайство о реализации» → склейка в сводный PDF (`pypdf`).
- **Экран дозаполнения** (`afd:isk_review` → `afd:isk_generate`): кредиторы с подсветкой пробелов реквизитов; **выбор анкеты** (если несколько), **СРО** (typeahead `/questionnaire/ref-search/?type=sro`), **место работы** (DaData party suggest), дозаполнение счетов/имущества/прежней фамилии, выбор приложений. Поля, которых нет в анкете (счета, детали имущества, № ИП), — только тут.
- **Результат** (`isk_generator.py:generate_isk`): PDF (заявление + приложения) + редактируемый .docx → папка «Заявления в суд» файл-менеджера; событийка `isk_created`.
- **Сидинг:** `python manage.py afd_isk_seed` / data-миграции `afd 0003–0005` — `IskTemplate` + 21 раздел (boilerplate ст. 213.4/213.6 + Пленум №45 из эталонов; разделы дальше правятся в UI). Подробности по структуре/источникам — память `afd-iskovoe-design`.

### Дальше по ТЗ (не сделано)
Генераторы запросов в госорганы, выбор контрагента и адреса, отправка по email, контроль ответов по времени, заказные письма Почтой РФ + трекинг РПО.

## Бэкапы / восстановление

```bash
# Ручной бэкап
docker compose -f <compose> --env-file <env> exec -T db pg_dump -U crm_user -d crm_db --no-owner --no-acl | gzip > backups/db-$(date +%Y%m%d-%H%M%S).sql.gz
# Восстановление
gunzip -c backups/db-XXXX.sql.gz | docker compose -f <compose> --env-file <env> exec -T db psql -U crm_user -d crm_db
# Автоматически: контейнер `backup` (ежедневно) + кнопка/handler `backup` в DevOps-панели
```

## Гайдлайны кода

- Комментарии и UI-тексты — на русском (как в существующем коде).
- HTMX-партиалы — в `templates/<app>/partials/`. daisyUI 4 классы, Tailwind pre-compiled (`static/css/tailwind.css` — не пересобирать без необходимости). Если добавил новые классы и нужна пересборка (Node на серверах нет): `docker run --rm -v "$(pwd)":/app -w /app node:20-alpine sh -c "npm install && npm run build"`, затем restart `web` (он сам сделает `collectstatic`). `node_modules/` в `.gitignore` — коммитить только `tailwind.css`.
- **При изменении стилей в шаблонах:** (1) проверить что используемые tailwind-классы **есть в `static/css/tailwind.css`** — `grep -F "md:grid-cols-3" static/css/tailwind.css`; если класса нет (особенно `md:*`/`row-span-*`/нетипичные числа) — либо пересобрать tailwind (см. выше), либо обойтись inline `style="..."`. (2) После правок **рестартнуть `web`** (`docker compose ... up -d --force-recreate web` или `restart web`) — Django в prod-настройках кэширует шаблоны (`cached.Loader`), и без рестарта изменения шаблона могут не подхватиться. (3) Пользователю напомнить про `Ctrl+Shift+R` — HTMX-партиалы тоже кэшируются в браузере.
- Структурированные типы вопросов анкеты: модель `QUESTION_TYPES` → `_extract_answer` ветка → partial-шаблон → JS add/remove/init в `dashboard.html` → `create_bfl_questionnaire.py` → пересоздать шаблон БФЛ.
- **Права в новом коде** — два уровня (детали в `guides/admin-overview.md`):
  - Role-based: `apps.core.permissions` — `is_admin`/`is_references_access`/`is_management`/`has_role`, декораторы `@require_*`, DRF-классы `ReadOnlyOrIsAdmin` и т.п. **Не** дублируй inline `emp.role in (...)` — всё уже есть.
  - Object-level: django-rules (`rules==3.5`). Предикаты и `add_perm` — в `apps/<app>/rules.py` (auto-discover). Сейчас покрыты `crm.view/edit/delete_client` и `crm.view/edit/delete_service`. Для фильтрации списков — менеджеры `Client.objects.visible_to(user)` / `Service.objects.visible_to(user)` (django-rules сам queryset не фильтрует). В шаблонах — `{% load rules %}` + `{% has_perm 'crm.edit_client' user client as can_edit %}`. Важно: правила в `visible_to` и в `rules.py` должны совпадать — менять синхронно.
  - Финансы используют свои `apps.finance.permissions` (доменные).
- Перед коммитом: `docker compose exec -T web python manage.py check`.
- **Если добавлена/изменена зависимость в `requirements.txt`** — на prod нужен `rebuild` (не `deploy`!). `deploy` не пересобирает образ и упадёт на migrate, потому что Django при старте импортирует `INSTALLED_APPS` (например, `rules.apps.AutodiscoverRulesConfig`).
- **JS/CSS — только локально из `/static/`, не CDN.** HTMX 1.9.8, ws.js, Twemoji лежат в `static/js/htmx-1.9.8.min.js`, `htmx-ext-ws.min.js`, `twemoji.min.js`. Не возвращать ссылки на unpkg/jsdelivr — убирает внешний RTT (важно на медленных корпоративных сетях) и риск блокировки CDN. Кэшируется навсегда через WhiteNoise `CompressedManifestStaticFilesStorage`.
- Ветка для prod-готового кода: `feat/production-ready` (слита в `main`). Коммиты — на русском, с `Co-Authored-By: Claude ...`.
- **VSCode-конфиг проекта**: в `.vscode/` шарятся только `settings.json` (Django-html ассоциации, Pylance в мягком режиме т.к. зависимости живут в Docker, `files.exclude`/`watcherExclude`/`search.exclude` под тяжёлые папки — staticfiles/node_modules/media/backups/*.min.js, per-language tabSize, tailwind для django-html) и `extensions.json` (Python/Pylance, batisteo.vscode-django, Docker, GitLens, Tailwind). Остальное (`launch.json` и личное) игнорируется — в `.gitignore` стоит `.vscode/*` + `!.vscode/settings.json` + `!.vscode/extensions.json`. Разработка через **Remote-SSH**: расширения ставятся на сервер один раз в `~/.vscode-server/extensions/` и автоматически общие для всех клиентов (рабочий+домашний комп). Локальные UI-настройки (тема, шрифт, keybindings) синхронизируются через **VSCode Settings Sync** с одним GitHub/Microsoft-аккаунтом на обеих локальных машинах.
- **`docker compose exec --env-file .env.dev` ловушка**: compose-плагин читает `env_file:` директивы из `docker-compose.prod.yml` (там подставляется `.env.prod`) даже при флаге `--env-file .env.dev`, и падает `env file /var/www/siricrm/.env.prod not found`. Воркэраунд для разовых команд — `docker exec siricrm-web-1 python manage.py check` напрямую (без compose-обёртки).

## Подробные документы

Интеграции со сторонними сервисами (детально):
- [`arbitr_integration_claude.md`](./arbitr_integration_claude.md) — мониторинг kad.arbitr.ru (Selenium+Xvfb, anti-bot)
- [`whatsapp_integration_claude.md`](./whatsapp_integration_claude.md) — WhatsApp Business через 1msg.io (sender, прокси медиа, webhook)
- [`telegram_integration_claude.md`](./telegram_integration_claude.md) — Telegram userbot (Telethon) + leads-bot (polling getUpdates)
- [`maxchat_integration_claude.md`](./maxchat_integration_claude.md) — MAX Bot API (3-step upload медиа, webhook, алёрт-канал арбитра)
- [`bubble_integration_claude.md`](./bubble_integration_claude.md) — импорт данных из bubble.io (BubbleRecord-буфер, appliers, доливка)
- [`scans_integration_claude.md`](./scans_integration_claude.md) — Входящие сканы: scan-agent (трей/автозагрузка), лоток `/scans/`, привязка по `scanner_name`

Техническое (`docs/`) — внутренняя архитектура (детали вынесены из CLAUDE.md):
- [`docs/auth-idle-ux.md`](./docs/auth-idle-ux.md) — multi-tab logout + idle UX + keepalive (с «не ломать»-инвариантами)
- [`docs/ui-conventions.md`](./docs/ui-conventions.md) — UI/UX-конвенции и гоччи (канбан-колонка, индикатор, файлы, чат-модалка)
- [`docs/crm-log-sobytiyka.md`](./docs/crm-log-sobytiyka.md) — лог клиента `ClientLogEntry` (СОБЫТИЙКА): модель, миграции, UI
- [`docs/kanban.md`](./docs/kanban.md) — канбаны: инбокс, передача услуги, права графика, DnD, карточки
- [`docs/find.md`](./docs/find.md) — поиск: глобальный поиск + фильтр канбана (похожие/«Только этот» 🎯, `cid`/`q`, кнопки действий, гоччи)
- [`docs/dashboard-widgets.md`](./docs/dashboard-widgets.md) — виджеты-счётчики дашборда (онлайн-сотрудники по heartbeat) + 🛑 гочча наследования `hx-target`
- [`docs/leads-routing.md`](./docs/leads-routing.md) — ClientPhone + маршрутизация лидов
- [`docs/client-dedup.md`](./docs/client-dedup.md) — дедуп клиентов (один телефон=один клиент), лайв-хинты ФИО/тел, нормализация +7 (XXX) XXX-XX-XX, единая кнопка «Сохранить», 🛑 гочча `<input type="date">` ISO (затирала даты)
- `docs/PRODUCTION.md` — развёртывание prod на 45.90.35.187
- `docs/DEV_MIGRATION.md` — перенос dev на новый сервер
- `docs/legacy-quickstart.md` — старый гайд по запуску (частично устарел)

Пользовательские инструкции (`guides/`):
- `guides/devops-panel.md` — как пользоваться DevOps-панелью (для суперюзера)
- `guides/admin-overview.md` — приложения, модели, права, сигналы, Celery beat — для понимания общей структуры
- `guides/finance-module.md` — финансовый учёт: модели, генератор графика, статусы, права, события

Прочее:
- `README.md` — общее описание проекта (для GitHub)
