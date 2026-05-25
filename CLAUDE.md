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
apps/crm            — клиенты, услуги, канбаны, лог событий (ClientEvent), API
apps/files          — файловый менеджер клиента (папки/дерево/превью), S3
apps/realtime       — WebSocket consumers (Telegram-чат, уведомления), channels
apps/telegram       — userbot (Telethon), бот, авторизация по TG
apps/maxchat        — интеграция MaxChat
apps/consultations  — график консультаций
apps/questionnaire  — анкеты БФЛ (типизированные вопросы), PDF через ReportLab, S3
apps/devops         — DevOps-панель (см. ниже)
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
- Handlers (см. `apps/devops/handlers/`): `status`, `backup` (pg_dump→gzip→S3+локально, отдаёт pre-signed download URL), `list_backups`, `s3_stats` (статистика бакетов по префиксам), `disk_usage` (df / + размеры репо-каталогов + Docker images/volumes/cache через docker.sock), `git_log` (последние коммиты — для выбора точки отката), `deploy` (git pull --ff-only + migrate + restart web/celery), `rollback` (git reset --hard на коммит + попытка авто-реверса миграций; отказ на грязном дереве), `rebuild` (git pull + docker compose build + up -d), `pull_db` (защитный бэкап dev → backup на источнике → restore на dev), `push_db` (бэкап dev → шлём цели `restore_db`), `restore_db` (на цели: защитный бэкап себя → скачать дамп → drop schema + restore), `dumpdata_tables`/`loaddata_tables` (выборочный sync по Django-моделям через JSON-фикстуры, UPSERT по pk — для справочников), `pull_tables`/`push_tables` (LOCAL-оркестраторы выборочного sync; на dev собирают dumpdata→S3→loaddata)
- **`pull_db`/`restore_db` переживают свой же drop schema**: до restore снапшотят `DevopsAction`+`DevopsAgentJob` (свою же tracking-запись) и Environment-записи; после restore возвращают через `update_or_create` + дёргают `manage.py devops_setup`. Иначе action висел running в UI вечно, а Environments на dev исчезали (если на источнике не настроены).
- Поток: dev-панель создаёт `DevopsAction` → либо локальный `DevopsAgentJob` в dev-runner (`pull_db`/`push_db`), либо HTTP на агента цели → его `DevopsAgentJob` в его runner. Для env `dev` HTTP идёт петлёй на `crmsiri.ru`.
- Опасные действия (`pull_db`, `push_db`, `deploy`, `rebuild`, `rollback`) требуют подтверждения в UI
- Синк статуса `DevopsAction`: HTMX-поллинг раз в 2с при открытой карточке + фоновый Celery-task `devops.sync_action` в dev-runner (общая логика — `sync_action_once` в `tasks.py`). Таск перепланирует сам себя `apply_async(countdown=3)` до done/failed, потолок 60 мин. Action закрывается даже если вкладка закрыта. Транзитивные DB-ошибки (БД схемы временно нет во время `pull_db`) — перепланируются, цепочка не обрывается.
- `action_poll` редиректит на `action_detail` при не-HTMX запросе — чтобы после логин-редиректа пользователь не приземлялся на голый партиал.
- `deploy`/`rebuild`/`rollback` НЕ перезапускают сам `devops-runner` — после деплоя нового кода на сервер его воркер остаётся на старом коде, нужен ручной `docker compose ... restart devops-runner` на этом сервере (иначе новые action_type / новые celery-tasks типа `sync_action` падают с «Неизвестный action_type» или просто не запускаются)

## Инфраструктурные особенности

- **VPN (Amnezia/WireGuard)** на обоих серверах — split-tunnel: через VPN идёт только трафик к Telegram-подсетям (`149.154.160.0/20`, `91.108.x.x/22`) и Anthropic (`160.79.104.10/32`, `34.36.57.103/32`), остальное напрямую. **Не ставить `AllowedIPs=0.0.0.0/0`** — порвётся SSH.
  - prod: обычный WG, интерфейс `claude0`, `/etc/wireguard/claude0.conf`, peer `72.56.73.137:37539`
  - dev: AmneziaWG (обфускация), интерфейс `awg0`, `/etc/amnezia/amneziawg/awg0.conf`, peer `72.56.73.137:33886`, systemd `awg-quick@awg0`
- **Telegram webhook'и не работают на наших серверах** — split-tunnel заворачивает ответный SYN-ACK на входящие от Telegram обратно в туннель, Telegram видит `Connection timed out`. Для приёма обновлений используем **polling** (`getUpdates`) через Celery beat — `apps/telegram/tasks.py:poll_telegram_leads` каждые 10с с long-poll timeout=20с и SETNX-локом `cache.add('telegram_leads:poll_lock')` (иначе параллельные таски ловят 409 Conflict). Чтобы выключить polling, не трогая код — `PeriodicTask.objects.filter(name='poll-telegram-leads').update(enabled=False)` (django-celery-beat хранит расписание в БД, beat подхватит за пару секунд).
- **S3 (Beget Cloud)**, endpoint `https://s3.ru1.storage.beget.cloud`, region `ru1`. Бакеты: prod media `1464bbae4a12-sirius-s3`, prod backups `1464bbae4a12-backup` (отдельные ключи `AWS_BACKUP_*`), dev media+backups `1464bbae4a12-siridev-s3`. **Beget валится на boto3 PUT (`XAmzContentSHA256Mismatch`)** — загрузка/скачивание только через pre-signed URL + `requests`.
- **Telegram userbot на dev** пока спит (нет credentials) — `userbot.py` gracefully выходит при пустых `TELEGRAM_PHONE`/`TELEGRAM_SESSION_STRING`.

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

## Bubble-импорт (`apps/bubble_import/`)

- **Лимит Bubble Data API на cursor-пагинацию — 50 000** записей. Сущности с большим объёмом (`MessageWSP`, `Files`) дофетчиваются **окнами по 30 дней** через `services.fetch_window()`. См. `tasks.WINDOWED_ENTITIES` + `WINDOW_YEARS_BY_ENTITY`. Идемпотентно через `update_or_create(entity, bubble_id)` — повторный fetch не дублирует.
- **`ProjectBFL.telWSP`** = WhatsApp-номер клиента по конкретной услуге. `apply_projectbfl` пишет его в `ClientPhone(purpose='whatsapp')` алиас — тот же клиент может писать с нескольких номеров.
- **WA-медиа vs Files**: `StoredFile.bubble_id` имеет префикс `wamedia_<msg_id>` для медиа из чатов и чистый bubble id для документов из таблицы `Files` — пересечений в БД нет (могут быть в S3 как двойные ключи, безвредно).
- **Management-команды** (для дочистки после массового импорта):
  - `sync_projectbfl_aliases` — пройтись по уже импортированным ProjectBFL и заполнить `ClientPhone(whatsapp)` из `raw.telWSP`.
  - `reapply_failed_wa` — сбрасывает MessageWSP с ошибкой «клиент не найден» в `pending+approved` и прогоняет; полезно после sync алиасов.
  - `create_leads_from_failed_wa` — для оставшихся непривязанных номеров создаёт клиентов-лидов (как при онлайн-обращении) и сбрасывает их сообщения в pending для повторного apply.
- **Долгие job'ы**: `full_import_task` имеет `time_limit=24h`. Для длинных apply'ев (часы) запускай через `docker compose exec -d web sh -c "nohup python manage.py apply_bubble <Entity> > /tmp/x.log 2>&1 &"` — переживёт SSH-разрыв.

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
- Ветка для prod-готового кода: `feat/production-ready` (слита в `main`). Коммиты — на русском, с `Co-Authored-By: Claude ...`.

## Подробные документы

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
