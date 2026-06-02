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

**Путь к репозиторию различается:** dev — `/var/www/siricrm`, prod — `/var/www/projects/siricrm` (и `HOST_REPO_DIR` в `.env.*` должен указывать на этот путь — нужен для `rebuild`). SSH dev→prod не настроен (с dev на prod не залогиниться по ключу).

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
apps/core           — сотрудники, отделы, дашборд-конфиг, health endpoint (/health/)
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
config/             — settings/, urls, asgi (ASGI: HTTP+WS через daphne), celery
templates/          — Django-шаблоны (проект НЕ использует base.html — dashboard.html самодостаточен)
docs/               — технические доки (deployment, migration, legacy quickstart)
guides/             — пользовательские инструкции (devops-panel.md и т.д.)
```

## DevOps-панель (apps/devops)

- UI на dev: `https://crmsiri.ru/devops/` (только `is_superuser`). Дашборд разбит по секциям: Состояние серверов · Базы данных · Деплой · S3 · История. Опасные действия — модалки подтверждения (ввод кодового слова; `dev→prod` ещё и чекбокс).
- HTTP-агент: `https://<env>/devops/agent/...` — Bearer-токен из env `DEVOPS_AGENT_TOKEN` целевого сервера (на dev в `.env.dev` есть `DEVOPS_AGENT_TOKEN_PROD` — токен прода; `Environment.agent_token_env` указывает, какую переменную брать). Окружения в БД: `dev` (этот сервер, был `self`) и `prod` — оба активны.
- Контейнер `devops-runner` — Celery worker (очередь `devops`), доступ к `docker.sock` + git/docker CLI + compose plugin; монтирует репо по `HOST_REPO_DIR`
- Handlers (см. `apps/devops/handlers/`): `status`, `backup` (pg_dump→gzip→S3+локально, отдаёт pre-signed download URL), `list_backups`, `s3_stats` (статистика бакетов по префиксам), `disk_usage` (df / + размеры репо-каталогов + Docker images/volumes/cache через docker.sock), `git_log` (последние коммиты — для выбора точки отката), `deploy` (git pull --ff-only + migrate + restart web/celery/celery-beat/**userbot**), `rollback` (git reset --hard на коммит + попытка авто-реверса миграций; отказ на грязном дереве), `rebuild` (git pull + docker compose build + up -d), `pull_db` (защитный бэкап dev → backup на источнике → restore на dev), `push_db` (бэкап dev → шлём цели `restore_db`), `restore_db` (на цели: защитный бэкап себя → скачать дамп → drop schema + restore), `dumpdata_tables`/`loaddata_tables` (выборочный sync по Django-моделям через JSON-фикстуры, UPSERT по pk — для справочников), `pull_tables`/`push_tables` (LOCAL-оркестраторы выборочного sync; на dev собирают dumpdata→S3→loaddata)
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

### Multi-tab logout
При закрытии **последней вкладки** SiriCRM шлёт POST `/accounts/logout/` через `sendBeacon` — чтобы сессия не висела. Логика в `static/js/multi-tab-logout.js`, подключён в `dashboard.html`, `arbitr/_layout.html`, `devops/_layout.html`. **Каждый layout обязан подключать этот скрипт** — иначе:
- Страница без heartbeat'а не появится в `localStorage.sirius_tabs` → при её закрытии счётчик «живых» вкладок неверный → ложный logout.
- ИЛИ закрытие такой страницы не отправит logout вообще.

> ⚠ **Гочча**: `beforeunload` срабатывает и на навигации по `<a href>` внутри сайта. Раньше переход `/dashboard/` → `/arbitr/` (любой full-page link) шёл с alive=0 (старая вкладка ещё не зарегистрировалась) → ложный sendBeacon('/accounts/logout/') → конкурентный GET /arbitr/ ловил `UpdateError: session was deleted` → 400. Лечение: при клике на `<a href>` того же origin или submit формы скрипт ставит `sessionStorage.sirius_internal_nav = Date.now()` — `beforeunload` видит метку (≤5 сек) и пропускает sendBeacon. Реальные закрытия (Alt+F4, кнопка ×) метку не ставят → logout уходит как раньше.

### Idle UX — warning + locked-overlay (без редиректа на /login/)

`IDLE_TIMEOUT_MINUTES = 10` (`config/settings/base.py`). Поток в `dashboard.html` (IIFE снизу) + `apps/core/views.py` + `apps/core/middleware.py`:

1. JS poller каждые **15с** → `GET /api/session/idle-check/` (этот путь в `IDLE_IGNORE_PREFIXES` middleware'а → НЕ обновляет `last_activity` и НЕ дёргает auto-logout сам).
2. Ответ: `{authenticated, idle_seconds, timeout_seconds, warning_seconds=60, logout_reason}`.
3. За **60с** до таймаута → **warning-модалка** (`#idle-warning`, z-index 9998) с countdown'ом и кнопкой «Остаться» (POST `/api/session/stay/` → обновляет `last_activity`).
4. После **600с** middleware (`IdleAutoLogoutMiddleware`) делает `auth_logout()` и кладёт `logout_reason` в сессию.
5. Следующий poll получает `authenticated=false` → **locked-overlay** (`#idle-locked`, z-index 9999) с inline-формой логина (POST `/api/session/login/` → `authenticate + login` в той же сессии, потом `window.location.reload()`).

**Keepalive: активность продлевает сессию через poll (НЕ отдельный интервал).** `last_activity` на сервере обновляет только non-ignored HTTP-запрос. Но клики/скролл/ввод/движение мыши, а особенно чат по WebSocket (`/ws/` в IGNORE) и чтение длинных страниц, HTTP не шлют → активного юзера выкидывало по таймауту. Решение: слушаем `mousedown/mousemove/keydown/touchstart/input/scroll/wheel` (capture-фаза → ловится и в `<dialog>`/модалках) и пишем `_lastActivity`; `poll()` шлёт `GET /api/session/idle-check/?a=1`, если активность была в окне опроса, а вьюха при `a=1` обновляет `last_activity` и возвращает `idle_seconds=0`. Привязка к НАДЁЖНОМУ поллеру обязательна: прежний отдельный `setInterval`-keepalive (POST `/stay/`) на практике почти не срабатывал (троттлинг фоновых вкладок, гонка с warning-модалкой) — на проде было ~0 вызовов `/stay/` против ~42 idle-check/сек. **Гочча:** юзеры с уже открытыми вкладками крутят старый JS до перезагрузки страницы — фикс активируется по мере reload/релогина.

**Guard от runaway:** IIFE начинается с `if (window.__siriIdleInit) return; window.__siriIdleInit = true;` — при `hx-boost`/повторной вставке скрипта IIFE не регистрировал второй `setInterval(poll)` (был runaway idle-check).

**Ключевые механизмы (всё в dashboard.html IIFE):**
- `visibilitychange` + `focus` → `poll()` сразу. Без этого browser throttle'ит `setInterval` в фоновых вкладках до 1/мин → юзер мог вернуться и кликнуть до того как poll увидит logout.
- Конец warning-countdown → `poll()` сразу (а не ждать 15с).
- **`htmx:beforeOnLoad`** — перехват ответов с `HX-Redirect: /accounts/login/` или статусом `401`. Вместо HTMX-редиректа на login (через `HtmxLoginRedirectMiddleware`) показываем locked-overlay. Этим закрываем гонку «юзер кликнул до того как poll увидел logout» — первый же XHR-ответ показывает оверлей.
- **Capture-phase trap** для `click` / `keydown` / `submit`: пока `_showLocked === true`, всё что не внутри `#idle-locked` блокируется. Защита от `<a href>` навигации (которая идёт мимо HTMX) и от элементов с z-index ≥ 9999.

**Эндпоинты `/api/session/`:**
- `idle-check/` — публичный (auth не обязателен), в `IDLE_IGNORE_PREFIXES`. При `?a=1` (клиент сообщил о реальной активности) обновляет `last_activity` и отдаёт `idle_seconds=0` — основной keepalive. Пустой поллинг (без `a=1`) активностью не считается.
- `stay/` — `@require_POST`, требует auth, обновляет `last_activity`. НЕ в IGNORE_PREFIXES. Сейчас используется только кнопкой «Остаться» в warning-модалке.
- `login/` — `@require_POST`, JSON `{username, password}` → `authenticate + login`. НЕ в IGNORE_PREFIXES.

> ⚠ **Карточки клиента как отдельной страницы НЕТ** — в `apps/crm/urls.py` нет паттерна `clients/<uuid>/` без суффикса. Клиент открывается через `{% url 'chat' client.id %}` (`/clients/<uuid>/chat/`) HTMX-swap'ом в `#content-area` дашборда. Прямой переход на чат-URL даст голый партиал без sidebar. Для ссылок «открыть клиента» из вне дашборда (например, из `/arbitr/`) пока показываем ФИО просто текстом — когда понадобится deep-link, сделать обработчик `/dashboard/?openClient=<uuid>` в `dashboard.html`.

## Инфраструктурные особенности

- **VPN (Amnezia/WireGuard)** на обоих серверах — split-tunnel: через VPN идёт только трафик к Telegram-подсетям (`149.154.160.0/20`, `91.108.x.x/22`) и Anthropic (`160.79.104.10/32`, `34.36.57.103/32`), остальное напрямую. **Не ставить `AllowedIPs=0.0.0.0/0`** — порвётся SSH.
  - prod: обычный WG, интерфейс `claude0`, `/etc/wireguard/claude0.conf`, peer `72.56.73.137:37539`
  - dev: AmneziaWG (обфускация), интерфейс `awg0`, `/etc/amnezia/amneziawg/awg0.conf`, peer `72.56.73.137:33886`, systemd `awg-quick@awg0`
- **Telegram webhook'и не работают на наших серверах** — split-tunnel заворачивает ответный SYN-ACK на входящие от Telegram обратно в туннель, Telegram видит `Connection timed out`. Для приёма обновлений используем **polling** (`getUpdates`) через Celery beat — `apps/telegram/tasks.py:poll_telegram_leads` каждые 10с с long-poll timeout=20с и SETNX-локом `cache.add('telegram_leads:poll_lock')` (иначе параллельные таски ловят 409 Conflict). Чтобы выключить polling, не трогая код — `PeriodicTask.objects.filter(name='poll-telegram-leads').update(enabled=False)` (django-celery-beat хранит расписание в БД, beat подхватит за пару секунд).
- **S3 (Beget Cloud)**, endpoint `https://s3.ru1.storage.beget.cloud`, region `ru1`. Бакеты: prod media `1464bbae4a12-sirius-s3`, prod backups `1464bbae4a12-backup` (отдельные ключи `AWS_BACKUP_*`), dev media+backups `1464bbae4a12-siridev-s3`. **Beget валится на boto3 PUT (`XAmzContentSHA256Mismatch`)** — загрузка/скачивание только через pre-signed URL + `requests`.
- **Telegram userbot на dev** пока спит (нет credentials) — `userbot.py` gracefully выходит при пустых `TELEGRAM_PHONE`/`TELEGRAM_SESSION_STRING`.
- **userbot держит Python-код в памяти** → при деплое с миграциями (например, рефактор моделей: `ClientEvent`→`ClientLogEntry`, дроп таблицы) контейнер на старом коде падает на несуществующих таблицах. Поэтому `siricrm-userbot-1` добавлен в `RESTART_CONTAINERS` deploy-handler'а (`apps/devops/handlers/deploy.py`) — деплой его рестартует вместе с web/celery. Рестарт безопасен: Telethon переподключается по `TELEGRAM_SESSION_STRING`. (Аналогичная ловушка была у userbot 01.06.2026 после деплоя СОБЫТИЙКИ.)

## Лиды / Телефоны / Маршрутизация (последний рефакторинг)

- **`crm.ClientPhone(client FK, phone, purpose)`** — единый источник правды по телефонам клиента. Назначения: `primary | whatsapp | telegram | max | additional`. UniqueConstraint `(phone, purpose)` — один номер на одну роль у одного клиента. `Client.phone`/`Client.whatsapp_phone` ОСТАЛИСЬ как кэш (пишутся синхронно), но **искать клиента нужно через ClientPhone**. Backfill из `Client.phone`→primary и `whatsapp_phone`→whatsapp сделан миграцией `crm.0065_backfill_client_phones_data`.
- **`apps/crm/phone_utils.py`** — единственная точка работы с номерами:
  - `normalize_phone(raw)` → E.164 без «+» (11 цифр, начинается с 7), или `""` если невалидно;
  - `find_client_by_phone(phone, purposes=None)` → Client | None — ищет по любому ClientPhone (с фильтром по purpose'ам если задано);
  - `add_client_phone(client, phone, purpose)` → ClientPhone | None — idempotent, возвращает None если номер уже занят другим клиентом в этом назначении;
  - `sync_client_phone_cache(client)` → пересчитывает `Client.phone`/`whatsapp_phone` из ClientPhone. Вызывать после CRUD телефонов.
- **`apps/crm/lead_routing.py`** — общая маршрутизация нового лида (используется и `apps/telegram/leads_bot.py` для TG, и `apps/whatsapp/views.py` для WA-webhook). `route_new_lead(client, source_label, event_description)` создаёт Service(БФЛ), привязывает к сотрудникам с галкой `Employee.accept_telegram_leads` (fallback — Власов Евгений по ФИО), ставит личный статус «Лиды из Telegram» в их «Мой канбан», пишет `ClientEvent(event_type='lead_received')` от имени системного «Бот Сириус» (`_system_bot_employee()` — без актёра событие выглядит обрезанным в UI/JSON).
- **Где искали клиента по номеру** (всё переведено на `find_client_by_phone`): WA-webhook (`apps/whatsapp/views.py:_get_or_create_wa_client`), TG-leads дедуп, `apply_messagewsp` (с fallback'ом на ClientPhone-алиасы — для исторического импорта). **Поиск в UI/API расширен `Q(phones__phone__icontains=q) + .distinct()`** — в 7 view-местах + ClientViewSet + admin search_fields.
- **`Employee.accept_telegram_leads`** (BooleanField) — у кого галка, тому летят TG/WA-лиды. Toggle в `templates/core/partials/admin_employees.html` через `core:admin_employee_toggle_tg_leads`. При включении автосоздаётся `ServiceEmployeeStatus(name='Лиды из Telegram')`.
- **WA-webhook автосоздаёт лида при незнакомом номере** (а не «unknown client» как раньше). Статус — `lead`, распределение через `route_new_lead`.

## WhatsApp интеграция через 1msg.io (apps/whatsapp)

**Полное описание:** [`whatsapp_integration_claude.md`](./whatsapp_integration_claude.md) — 1msg.io API, webhook flow, data:URI base64 для исходящих медиа, прокси `/wa/file/` (резерв), кириллица, TEST_MODE, env-vars, curl-шпаргалка.

Кратко: тонкий слой `apps/whatsapp/` (config+sender+tasks+views+middleware, без своих моделей) над общими Client/Message/StoredFile. Боевой канал клиента — JWT-токен 1msg в `WHATSAPP_API_TOKEN`. Все медиа уходят через `sendFile`. **Исходящие медиа** — `data:<ctype>;base64,<...>` в payload (после нескольких итераций URL-режима — прокси, headers, лимиты, кириллица). Прокси `/wa/file/<uuid>/` оставлен в коде на случай URL-режима. UI-кнопка `#btn-send-whatsapp` в `telegram_chat_panel.html` — проверять в `htmx:afterRequest`, иначе форма не очищается после отправки.

## Telegram интеграция (apps/telegram)

**Полное описание:** [`telegram_integration_claude.md`](./telegram_integration_claude.md) — userbot (Telethon) + leads-bot, polling vs webhook (split-tunnel ловушка), SETNX-лок, отправка из CRM, env-vars, отключение polling через django-celery-beat.

Кратко: **два TG-канала** — userbot (Telethon, основной аккаунт компании для CRM-чата) и leads-bot (отдельный bot-account, мониторит канал с лидами лендинга). Webhook не работает из-за WireGuard split-tunnel → **polling getUpdates через Celery beat каждые 10с** с SETNX-локом (иначе 409 Conflict). Userbot — отдельный контейнер `userbot`. UI-кнопка `#btn-send-telegram` в чате. Лиды → `route_new_lead("Telegram", ...)` на сотрудников с `Employee.accept_telegram_leads`.

## MAX интеграция (apps/maxchat)

**Полное описание:** [`maxchat_integration_claude.md`](./maxchat_integration_claude.md) — MAX Bot API endpoint'ы, 3-step upload медиа (uploads → PUT → wait_attachment_ready → messages), webhook, использование как алёрт-канала для арбитража.

Кратко: тонкий слой `apps/maxchat/` (sender+tasks+views, без своих моделей) над `Client/Message/StoredFile`. MAX Bot API `https://platform-api.max.ru` — auth header `Authorization: <MAX_BOT_TOKEN>` без Bearer. Отправка медиа: 3 шага (`POST /uploads` → `PUT <url>` → `POST /messages`). `Client.max_chat_id` обязателен. UI-кнопка `#btn-send-max` в чате. Также используется арбитром для алёртов о капче (`ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID`).

## Права видимости клиентов (настраиваются в UI)

Логика в `Client.objects.visible_to(user)` (`apps/crm/managers.py`). Видят ВСЕХ клиентов:
- `is_admin` / `is_superuser` / `managing_partner` / `head_dep`;
- `Employee.is_owner=True` (root-флаг для основателя — ставится только суперюзером в карточке сотрудника);
- сотрудник отдела с `Department.sees_all_clients=True` (например, «Отдел продаж»).

Остальные сотрудники видят клиента, если: они в `Client.employees` (ответственный), ИЛИ в `Service.employees` (исполнитель), ИЛИ у клиента есть `Service` с `common_status.department == их отдел` (этап обслуживания закреплён за их отделом через `ServiceCommonStatus.department`).

Helper `apps.core.permissions.can_view_all_clients(user)` + шаблонный фильтр `{% load permissions_tags %}` `{{ user|can_view_all_clients }}` — единая точка проверки «может смотреть всю компанию». Использовано в чат-фильтре «Все» (видна только management + `sees_all_clients`) и в backend-защите `telegram_clients_list` (scope='all' для остальных принудительно режется до 'mine').

Фильтры чат-панели «Мои» / «Отдел» учитывают и `Client.employees`, и `Service.employees` (через `Q(...)|Q(...).distinct()`).

## UI/UX-конвенции (последний UI-проход)

- **Канбан-колонка** (`apps/crm/views.py:kanban_column`): без `list(qs)` (тащило весь queryset в память) — `qs.count() + qs[offset:offset+N]`. Авто-подгрузка через `hx-trigger="intersect root:#kanban-<status> threshold:0.1, click"` (закрытый root — обязательно через `#id`, **не** `closest .selector` — HTMX 1.9.8 в `intersect root:` принимает только чистый CSS-селектор). Индикатор — `hx-indicator="#kanban-<status>"`, CSS `.kanban-col-body.htmx-request::before` (sticky-spinner по центру видимой области).
- **Канбан-карточка**: primary-телефон с иконкой и `tel:`; последнее сообщение клиента — через `Subquery(Message.objects.filter(client=OuterRef('pk')).order_by('-created_at').values('content')[:1])`, чтобы избежать N+1.
- **Файловый менеджер**:
  - `contents.html` (HTMX-партиал для tree-кликов) и `contents_inner.html` разделены: oob-обёртка vs «начинка». При первичном `{% include %}` в `manager.html` использовать **только `contents_inner.html`** — HTMX при beforeend-вставке изымает любой `hx-swap-oob` элемент, что ломало первичный рендер.
  - `?file=<uuid>` к `files:manager` — открывает менеджер сразу в папке файла + автораскрытие дерева до `.tree-item--active` (CSS `tree-children { display:none }` принудительно `display:block` для родителей) + подсветка строки (`.files-row--highlight`, pulse-анимация). Скрипт автораскрытия — `window.filesManagerOpenToActive` в `dashboard.html`, вызывается через `body.addEventListener('htmx:afterSettle', ...)` (HTMX **не** выполняет inline `<script>` в swap'нутом HTML, поэтому скрипт должен жить вне partial'а).
  - **Office-предпросмотр**: `_PREVIEWABLE['office'] = {doc,docx,xls,xlsx,ppt,pptx}`. Шаблон рендерит iframe c `view.officeapps.live.com/op/embed.aspx?src=<urlencoded>`. Работает потому что Beget pre-signed URL публично доступен.
  - **PDF-предпросмотр inline**: Beget по умолчанию отдаёт `Content-Disposition: attachment`. `get_presigned_url(..., inline=True, content_type=..., filename=...)` добавляет `ResponseContentDisposition='inline; filename=...'` и `ResponseContentType=<orig>` — браузер рендерит в iframe вместо скачивания. Применяется только для kind in {image,pdf,video,audio}.
- **Глобальный поиск**: расширен на ClientFile.name (мультислово AND через .split() + цепочка .filter). Каждой записи в результатах view проставляет `c.no_access = c.id not in Client.objects.visible_to(user).values_list('id', flat=True)`. Шаблон затеняет такие строки (`.gs-no-access`) и заменяет onclick на `globalSearchNoAccess()` → `showToast(...)`.
- **Toast-уведомления**: `window.showToast(msg, type)` в `dashboard.html` — slide-in карточка в правом нижнем углу, type ∈ `info|success|warning|error`. Использовать вместо `alert()`.
- **Кэш статики**: `STORAGES` (Django 4.2+ формат) обязателен в `config/settings/base.py`, иначе `STATICFILES_STORAGE = 'whitenoise...CompressedManifestStaticFilesStorage'` тихо игнорируется в Django 5.x и hash-имена не генерируются — `immutable`-кэш браузера держит CSS «навечно», `Ctrl+Shift+R` не помогает.
- **Шаблонные комментарии**: Django `{# ... #}` поддерживает только **одну** строку — multiline `{# ... \n ... #}` рендерится как текст. Использовать `{% comment %}...{% endcomment %}` для многострочных.
- **Чат-модалка (`#telegram_chat_modal` в dashboard.html)**:
  - Список клиентов слева **НЕ грузится на init дашборда** (раньше был `hx-trigger="load"` → 309мс CPU + 23КБ HTML впустую на каждом визите). Сейчас грузится лениво при первом открытии модалки через `htmx.ajax(...)` в `openTelegramChatModal()` (флаг `list.dataset.loaded`).
  - **`window._activeTelegramClientId`** — единственный источник правды о подсветке. `setActiveTelegramClient(el)` обновляет его при клике, `_applyActiveTelegramClient()` восстанавливает подсветку после любого `htmx:afterSwap` списка (search/scope/pagination). `htmx:afterSwap` сам выбирает: если id задан — подсветить именно его и НЕ трогать правую колонку; если null — старое поведение «подсветить первого + загрузить его чат».
  - **`?pin_client_id=<uuid>`** на `/telegram/clients/` — backend (`telegram_clients_list`) гарантирует, что указанный клиент попадёт в результат page=1 даже если не в текущем scope/search (через `Client.objects.visible_to(user)` + prepend в `page_obj.object_list`). Используется в `openTelegramChatModalForClient(id)` — клик по 💬 в kanban-карточке клиента из другого scope теперь корректно подсвечивает его слева + скроллит в видимую область.

## Лог клиента: события + действия (`ClientLogEntry`)

Единый лог в `apps/crm/models.py:ClientLogEntry`. Концепция:
- **Событие (kind='event')** — что произошло. Атрибут `event_type` → `EventType`. Источник в `EventType.source ∈ {system, court, client, legal_entity, employee}`.
- **Действие (kind='action')** — что сделал сотрудник. Атрибут `action_type` → `ActionType`. У ActionType есть `spawns_event` (FK → EventType) — при записи действия автоматически создаётся событие этого типа с `parent` = это действие. Используется для пар вроде «`service_create` (action сотрудника) → `service_created` (event, на который реагируют другие)».
- **`EventType.standard_actions`** (M2M → ActionType) — стандартный набор действий по событию. UI модалки показывает их как chips-подсказки.
- **`subject_kind ∈ {client, company, employee}`** + `client` FK / `subject_employee` FK. Сейчас заполняется только Client; Company/Employee subjects — задел на потом.
- **`parent`** (FK self) — связь action ↔ event (action-в-ответ-на-event или event-порождённый-action).

**Хелпер `apps/crm/client_log.py`** (импортировать `from apps.crm import client_log`, **не** ClientLogEntry напрямую):
- `record_event(client, code, *, comment="", employee=None, parent=None, old_value="", new_value="", bubble_id=None)` — записать событие.
- `record_action(client, code, ...)` — записать действие, и если у его ActionType задан `spawns_event` — auto-create связанное событие (`parent` = это действие).
- `record_legacy(client, event_type, description=..., ...)` — совместимость со старым API (legacy строковый event_type), сам резолвит kind+FK через внутренний `_LEGACY_MAP`. Используется в нескольких legacy-местах (finance/views.py:_log_event, questionnaire/views.py:_log_questionnaire_event).
- `invalidate_cache()` — сбросить кэш справочников (дёргается из CRUD `/references/event-type/`, `/action-type/`).

**История миграции** (`crm.0070`/`0071`/`0072`): создание моделей → seed справочников (22 EventType + 26 ActionType) → копирование старых 31991 `ClientEvent` в `ClientLogEntry` с маппингом → `DeleteModel('ClientEvent')`. См. `_LEGACY_MAP` в `client_log.py` и `STATUS_MAP` в `0071_seed_and_migrate_log.py`. Маппинг согласован с пользователем (см. сессию 29 мая 2026) — например `iskotpravlen` слит в action `claim_filed`, `call_outgoing`+`call_result` слиты в action `call_client`, fallback `system`-записи «Добавлен в базу» → event `client_created`.

**UI справочников** — `/references/event-types/` и `/references/action-types/` (apps/core/views.py). События сгруппированы древовидно по источнику через `{% regroup %}` + `<details>`. Сортировка столбцов — JS `window.sortRefTable(th, 'num'|'str')` в `templates/core/references_panel.html`. **Системные** типы (`is_system=True`) защищены: код read-only в форме, кнопка удаления скрыта, помечены 🔒.

**Доработки СОБЫТИЙКИ (UI + модель):**
- `ClientLogEntry.stored_file` (FK→StoredFile, мигр. `crm.0073` + `0074`-бэкофилл) — события «Получен/Отправлен файл» хранят сам файл; в модалке выводится имя + ссылка на предпросмотр (`files:stored_download?inline=1` — presigned с `Content-Disposition: inline`).
- `EventType.is_manual` / `ActionType.is_manual` (мигр. `crm.0075`) — «доступно для ручного добавления». Форма добавления в модалке показывает только `is_manual=True`; дефолт — действие **«Комментарий сотрудника»** (`employee_comment`). Галка `is_manual` редактируется в справочниках типов. `is_system` — только про защиту от удаления, на ручной выбор не влияет.
- Модалка: строки-карточки с «шапкой» (фон; время·тип·источник·ФИО в одну строку) + тело (файл/коммент/чипсы); typeahead-комбобокс выбора типа; добавление записи — append в ленту без ребилда модалки + плавный скролл; запись-ответ показывает «↳ в ответ на: ‹тип› · ‹время›» с переходом к родителю; фильтр/поиск form-level (`hx-trigger="change, keyup changed, search"`, `onsubmit="return false"`, `#log-q` для сохранения фокуса); «Источник: Сотрудник» = все действия.

**Модалка лога** (`templates/crm/partials/client_events_modal.html`) — открывается через `GET /clients/<uuid>/events/` (с фильтрами `?kind=&source=&type=&q=`); добавление через `POST /clients/<uuid>/events/add/` (поля `entry_kind`, `type_code`, `comment`, `parent_id` + эхо фильтров). Размер фиксированный — `width:95vw; max-width:1600px; height:90vh`. Лента в стиле чата (старое сверху, новое снизу, auto-scrollTop = scrollHeight при открытии). Цветовое кодирование: события — тёмно-синий шрифт `#1e3a8a`, действия — тёмно-зелёный `#166534` (Tailwind blue-900 / green-800 inline, классы не нужны).

## Канбаны: инбокс «Не принято», передача услуги, права на график

- **Инбокс «Не принято»** — универсальная личная колонка «Моего канбана»: `ServiceEmployeeStatus.is_inbox=True`, `common_status=None` (поле сделано nullable). Один на сотрудника (partial-unique `unique_inbox_status_per_employee`), заведён всем активным мигр. `crm.0076`/`0077`. Хелпер `apps/crm/kanban_inbox.py:ensure_inbox_status()` + `post_save`-сигнал на `core.Employee` (`apps/core/signals.py`) — новым сотрудникам инбокс создаётся автоматически. В `my_kanban` рендерится отдельной группой «📥 Входящие» сверху (`kanban_my.html`, `group.is_inbox`).
- **Передача услуги «В работу отдела/сотрудника»** (`apps/crm/service_transfer.py`): услуга кладётся в инбокс «Не принято» получателя(ей). **`Service.common_status` при передаче НЕ меняется** (логику смены статуса пропишем отдельно). Получатели — ровно два ограничения: действующий (`is_active`) + работает с услугой (`Employee.services_allowed` содержит `ServiceName`) → `eligible_employees(service)`. Иных проверок нет — НЕ придумывать. Переезд: прежние `ServiceEmployeeState`+M2M снимаются (кроме актора, если снята галочка «У меня завершить» → `keep_actor`). Поле «Комментарий» идёт в событийку. Лог: ActionType `service_transfer` (мигр. `crm.0078`) + событие `dept_assigned`/`employee_assigned`. Кнопка в модалке услуги (`form_modal.html`), пикер `service_transfer_modal.html`, вьюхи `service_transfer_modal`/`service_transfer`. **Принятие**: вытащил услугу из инбокса в рабочую колонку (`service_my_move`) → у остальных сотрудников она удаляется из инбокса.
- **График платежей — права** (`apps/finance/permissions.py:can_edit_schedule`): просмотр графика — всем; редактирование (POST графика + `charge_add`/`charge_edit`) — суперюзер, роль `admin` и сотрудники отделов с флагом `Department.can_edit_payment_schedule` (мигр. `core.0018`, засидован «Отдел продаж БФЛ»+«Бухгалтерия»). В модалке для не-редакторов — баннер «Только просмотр», `fieldset disabled`, скрытые кнопки.
- **Поиск на канбанах**: верхнее поле `#flt-q` (событие `kanbanRefresh`) работает на всех трёх канбанах. Колонки канбана услуг (`services_kanban_column`) и «Моего канбана» (`my_kanban_column`) слушают `kanbanRefresh`, включают `#flt-q` и фильтруют по `q` (ФИО/телефон клиента, мультислово AND). Включается только `#flt-q`, не вся форма фильтров.
- **Карточка канбана клиентов**: рядом с услугой показывается её общий статус («БФЛ — Консультация»), `services__common_status` в prefetch.
- **Значки каналов Т/М/W** в левом списке чата (`telegram_clients_list`): Т=Telegram (синий), М=MAX (розовый), W=WhatsApp (зелёный) — цветной символ+бордюр если из канала были сообщения, серый если нет. Каналы считаются одним запросом на страницу (`distinct client_id+channel`).

## Bubble-импорт (`apps/bubble_import/`)

**Полное описание:** [`bubble_integration_claude.md`](./bubble_integration_claude.md) — лимит cursor-пагинации 50k + окна 30 дней, management-команды (`sync_projectbfl_aliases`, `reapply_failed_wa`, `create_leads_from_failed_wa`, `fetch_bubble_since`, `backfill_status_from_bubble`), live vs `/version-test/`, гочча с `BubbleRecord.target_id` (str vs UUID).

Кратко: перенос данных с bubble.io в SiriCRM через BubbleRecord-буфер (JSONB raw + appliers). Боевой URL `BUBBLE_API_BASE=https://siricrmdev.ru/api/1.1/obj` — НЕ `/version-test/`. Долгие apply'и — через `docker compose exec -d web nohup ...`.

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

Техническое (`docs/`):
- `docs/PRODUCTION.md` — развёртывание prod на 45.90.35.187
- `docs/DEV_MIGRATION.md` — перенос dev на новый сервер
- `docs/legacy-quickstart.md` — старый гайд по запуску (частично устарел)

Пользовательские инструкции (`guides/`):
- `guides/devops-panel.md` — как пользоваться DevOps-панелью (для суперюзера)
- `guides/admin-overview.md` — приложения, модели, права, сигналы, Celery beat — для понимания общей структуры
- `guides/finance-module.md` — финансовый учёт: модели, генератор графика, статусы, права, события

Прочее:
- `README.md` — общее описание проекта (для GitHub)
