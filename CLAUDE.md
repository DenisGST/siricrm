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
- Handlers (см. `apps/devops/handlers/`): `status`, `backup` (pg_dump→gzip→S3+локально, отдаёт pre-signed download URL), `list_backups`, `s3_stats` (статистика бакетов по префиксам), `disk_usage` (df / + размеры репо-каталогов + Docker images/volumes/cache через docker.sock), `git_log` (последние коммиты — для выбора точки отката), `deploy` (git pull --ff-only + migrate + restart web/celery), `rollback` (git reset --hard на коммит + попытка авто-реверса миграций; отказ на грязном дереве), `rebuild` (git pull + docker compose build + up -d), `pull_db` (защитный бэкап dev → backup на источнике → restore на dev), `push_db` (бэкап dev → шлём цели `restore_db`), `restore_db` (на цели: защитный бэкап себя → скачать дамп → drop schema + restore), `dumpdata_tables`/`loaddata_tables` (выборочный sync по Django-моделям через JSON-фикстуры, UPSERT по pk — для справочников), `pull_tables`/`push_tables` (LOCAL-оркестраторы выборочного sync; на dev собирают dumpdata→S3→loaddata)
- **`pull_db`/`restore_db` переживают свой же drop schema**: до restore снапшотят `DevopsAction`+`DevopsAgentJob` (свою же tracking-запись) и Environment-записи; после restore возвращают через `update_or_create` + дёргают `manage.py devops_setup`. Иначе action висел running в UI вечно, а Environments на dev исчезали (если на источнике не настроены).
- Поток: dev-панель создаёт `DevopsAction` → либо локальный `DevopsAgentJob` в dev-runner (`pull_db`/`push_db`), либо HTTP на агента цели → его `DevopsAgentJob` в его runner. Для env `dev` HTTP идёт петлёй на `crmsiri.ru`.
- Опасные действия (`pull_db`, `push_db`, `deploy`, `rebuild`, `rollback`) требуют подтверждения в UI
- Синк статуса `DevopsAction`: HTMX-поллинг раз в 2с при открытой карточке + фоновый Celery-task `devops.sync_action` в dev-runner (общая логика — `sync_action_once` в `tasks.py`). Таск перепланирует сам себя `apply_async(countdown=3)` до done/failed, потолок 60 мин. Action закрывается даже если вкладка закрыта. Транзитивные DB-ошибки (БД схемы временно нет во время `pull_db`) — перепланируются, цепочка не обрывается.
- `action_poll` редиректит на `action_detail` при не-HTMX запросе — чтобы после логин-редиректа пользователь не приземлялся на голый партиал.
- `deploy`/`rebuild`/`rollback` НЕ перезапускают сам `devops-runner` — после деплоя нового кода на сервер его воркер остаётся на старом коде, нужен ручной `docker compose ... restart devops-runner` на этом сервере (иначе новые action_type / новые celery-tasks типа `sync_action` падают с «Неизвестный action_type» или просто не запускаются)

## Арбитраж — мониторинг kad.arbitr.ru (apps/arbitr)

**Цель:** автоматически следить за делами клиентов БФЛ на kad.arbitr.ru. Сотрудник в карточке услуги ставит «иск отправлен в суд» → создаётся `ArbitrCase(status='searching')` → парсер ищет дело по ФИО → сотрудник подтверждает найденную карточку → `status='monitoring'` → парсер регулярно собирает события (ходатайства, определения, расписания заседаний) и документы.

### Контейнер `arbitr-runner` (отдельный)

- `docker/arbitr/Dockerfile` — python:3.11-slim + **google-chrome-stable** (из официального deb-репо) + **chromedriver** (под мажор-версию Chrome, тянем с chrome-for-testing) + **Xvfb** (виртуальный X-сервер). selenium 4.27 + selenium-stealth.
- `docker/arbitr/entrypoint.sh` — поднимает Xvfb на `:99` в фоне → `exec celery worker -Q arbitr`. `ENV DISPLAY=:99` в Dockerfile — чтобы и worker, и `docker exec` (kad_probe) ходили на один Xvfb.
- Сервис в обоих compose-файлах: `shm_size: '2gb'`, `concurrency=1`, `max-tasks-per-child=20` (Chromium течёт).
- Очередь celery `arbitr` — `CELERY_TASK_ROUTES['arbitr.*']={'queue':'arbitr'}` в `base.py`. Общий celery worker эти таски не подхватит.

### Почему Selenium, а не Playwright

Сначала пробовал Playwright headless (jammy/noble образы microsoft/playwright-python). **Не пошло**: kad возвращал captcha challenge на любой data-запрос (`/Card/<uuid>`, `/Kad/SearchInstances`) — даже с `playwright-stealth`. Главная страница отдавалась (она статика), но контент — никогда. Selenium + selenium-stealth с **headed Chromium через Xvfb** проходит anti-bot kad.

### Антидетект-стек (`apps/arbitr/parsers/kad.py:KadSession`)

- **headed Chrome через Xvfb** (НЕ headless) — главное оружие.
- `selenium-stealth(vendor='Google Inc.', platform='Win32', webgl_vendor='Intel Inc.', renderer='Intel Iris OpenGL Engine')` — глубже подделывает navigator + WebGL, чем playwright-stealth.
- Chrome args: `--disable-blink-features=AutomationControlled`, `excludeSwitches=['enable-automation']`, `useAutomationExtension=False`, `--disable-webgl --disable-gpu --enable-unsafe-swiftshader --disable-accelerated-2d-canvas` (kad фингерпринтит WebGL renderer).
- UA `Mozilla/5.0 (Windows NT 10.0; Win64; x64) … Chrome/120` — Linux-UA подозрительный для kad.

### Главный трюк: «прогрев» сессии

Прямой `GET /Card/<uuid>` всегда возвращает captcha. Но если в той же Chrome-сессии сначала сделать **поиск через UI** (главная → ввод в `[placeholder="например, А50-5568/08"]` → клик `[alt="Найти"]` → ждать `.b-case-loading` 10-15с) — kad ставит session-cookies и дальше отдаёт `/Card/` без капчи. Это и есть `KadSession._warm_up()` — один раз на жизнь сессии, потом много операций.

### Capture detection

Маркеры в `apps/arbitr/parsers/kad.py:CAPTCHA_MARKERS`:
- `id="tokenFrom"` — форма submit'а captcha challenge
- `pravocaptcha.execute` — JS-вызов на странице challenge
- `"Доступ заблокирован"` — IP-блок (HTTP 451)
- `"Подтвердите, что вы не робот"`

**НЕ маркер** — `b-pravocaptcha` сам по себе или текст «Превышено количество попыток»: оба присутствуют в обычной kad-странице как `<script type="x-jquery-tmpl">`-шаблон → ложные срабатывания.

### Селекторы (по состоянию 2026)

Search results (таблица `#b-cases` tbody, **не** `b-cases tablesorter`):
- `td.num a.num_case` — номер дела + ссылка
- `td.num .b-container > div > span` — дата подачи
- `td.court .judge` — судья (отдельно)
- `td.court .b-container > div:not(.judge)` — суд (отдельно)
- `td.plaintiff .js-rollover` / `td.respondent .js-rollover` — стороны (внутри есть скрытый `.js-rolloverHtml`-tooltip → берём только `firstChild.textContent` через JS)

Карточка дела:
- `#caseName` (hidden input) — case_number (надёжнее `<title>`)
- `#caseId` (hidden input) — case_id
- `.b-case-header-desc` — статус
- `.b-chrono-item-header` — инстанции (13+ штук, со всеми реквизитами в `.l-col strong` / `.b-case-instance-number` / `.instantion-name`)
- Раскрытие инстанции: клик по `.b-collapse .b-sicon` (через `execute_script` — обходит overlap)
- События: `.b-chrono-item.js-chrono-item` (БЕЗ `-header`) — внутри `.case-date`, `.case-type`, `.case-subject`, `.additional-info`, `.b-case-result-text`. `data-instance_id` связывает событие с инстанцией.

### Идемпотентность событий

У kad `data-id` на событии **всегда пуст** — поэтому `kad_event_id` генерируем как `sha1(instance_id|event_date|kind|title|description)[:24]`. UNIQUE-индекс `(case, kad_event_id)` в `ArbitrEvent.Meta.constraints` защищает от дублей при повторном парсинге.

### Восстановление судьи

kad на уровне шапки инстанции имя судьи не пишет — но в каждом событии `kind == "Событие"` ФИО судьи лежит в `case-subject`. `_fill_instance_judges` берёт самое частое ФИО (regex `^[А-Я][а-я]+(\s+[А-Я]\.\s*[А-Я]\.?)?$`) в рамках инстанции.

### Окно работы + ручной запуск

- Автотаски `arbitr.kad_monitor_pending` / `kad_monitor_case` работают **только 18:00–08:00 МСК** (см. `WORK_WINDOW_*` в `tasks.py`).
- **Ручной запуск** через UI кнопку «Парсить сейчас» → таск `arbitr.kad_monitor_one_case(case_id)` → **work-window игнорируется**. Используется для отладки и срочного «парсить сейчас». В зависимости от status: `_search_one` (SEARCHING) или `_parse_one` (MONITORING).

### MAX-уведомления о капче

`apps/arbitr/notifications.py:send_captcha_alert(case)` шлёт сообщение в MAX через `apps.maxchat.sender.send_max_message` с `ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID` (env, пока один на всех получатель — `chat_id` админа; позже разнесём на `Employee.max_chat_id`). Текст: дело, ФИО клиента, сотрудник запустивший мониторинг, ссылка на kad.

### Сервисная UI `/arbitr/`

- `views.dashboard` — три секции: 🔍 Этап 1 (status=searching, поиск по ФИО), 📋 Этап 2 (monitoring), ⏸ Приостановленные/завершённые. Доступ — `is_admin`. Карточка дела: клиент, статус последнего лога (✓/⚠/✗), `last_check_at` через `naturaltime`, `next_check_at ≈` (расчёт `last + 1 час`, если попадает в work-window, иначе следующее 18:00), счётчики `events_count` / `attachments_count` (annotate Count + Q). У SEARCHING-карточек с непустым `search_hits` — бейдж «🎯 кандидатов: N».
- `views.case_detail` — шапка дела + блок «Найденные кандидаты» (для status=searching) + список инстанций + хронология `ArbitrEvent` (последние 200) с документами + лог `ArbitrCheckLog` (50).
- **Ручной запуск** — POST на `/arbitr/case/<uuid>/run/` → `kad_monitor_one_case.delay()` → возврат свежей карточки (HTMX `hx-swap="outerHTML"`).
- **HTMX-поллинг лога** раз в 4с пока последний `ArbitrCheckLog.ts` моложе 60с. **Решение «поллить или нет» — на сервере**: view возвращает партиал с `hx-trigger` атрибутом ИЛИ без него (см. `poll_active` в `views._log_is_fresh`). Так поллинг сам останавливается без JS-фильтров.
- Меню — `MenuItem(section='Арбитраж', name='Мониторинг дел', url='/arbitr/', icon='landmark')`, миграция `core.0016_arbitr_menu_item` + `0017_arbitr_menu_attach_to_configs` (см. ловушку ниже).
- **Шаблоны используют `{% load humanize %}`** (для `naturaltime`) — `django.contrib.humanize` должна быть в `INSTALLED_APPS` (иначе `TemplateSyntaxError` → HTTP 500). Лежит в `config/settings/base.py` рядом с `staticfiles`.

### Confirm-flow: SEARCHING → MONITORING через UI

- `_search_one` пишет `KadSearchHit[]` в `ArbitrCase.search_hits` (JSONField) + `search_hits_at` (DateTime). UNIQUE-индекс на (case, kad_event_id) уже стоит — повторный поиск перезаписывает hits.
- `case_detail.html` для status=searching рендерит блок «🎯 Найденные кандидаты»: каждый hit (`case_number`, `court_name`, `parties`, `filed_at`) + кнопка «✓ Это моё дело» (form POST → `/arbitr/case/<uuid>/confirm-hit/<int:index>/`).
- `views.case_confirm_hit` валидирует индекс, выставляет `case_number/kad_url/court_name` из hit'а, переводит case в MONITORING, очищает search_hits, пишет `ClientEvent(event_type='iskotpravlen')` + `ArbitrCheckLog`. HX-Redirect (для HTMX) / 302 (full submit) → full page reload, т.к. рендер карточки полностью меняется.

> ⚠ **Ловушка для новых пунктов sidebar**: `context_processors.sidebar_menu` рендерит **не все** `MenuItem`, а только привязанные к `DashboardConfig.menu_items` (M2M) у конфига сотрудника. Создания `MenuItem.objects.create(...)` **недостаточно** — нужно ещё пройтись по `DashboardConfig.objects.all()` и сделать `.menu_items.add(item)`. См. `0017_arbitr_menu_attach_to_configs` как образец.

### Команды отладки

```bash
# открыть главную (быстрый чек что Chrome+Xvfb+stealth поднимаются)
docker exec siricrm-arbitr-runner-1 python manage.py kad_probe open
# поиск через UI (с прогревом)
docker exec siricrm-arbitr-runner-1 python manage.py kad_probe search "А12-33291/2024"
docker exec siricrm-arbitr-runner-1 python manage.py kad_probe search "Иванов Иван Иванович" --court А12
# парсинг карточки
docker exec siricrm-arbitr-runner-1 python manage.py kad_probe case https://kad.arbitr.ru/Card/<uuid>
```

### Env-vars

- `ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID` — куда слать алёрт о капче (один на всех).
- `ARBITR_HEADLESS=true|false` — для локальной отладки парсера без Xvfb (по умолчанию `true` — но Chrome в нашем контейнере фактически headed через Xvfb, флаг управляет только `--headless=new` arg).

### Скачивание PDF — особая защита kad

Endpoint `/Kad/PdfDocument/<case>/<doc>/<filename>.pdf` нельзя дёрнуть напрямую — kad отдаёт **ПравоКапчу** (`<form id="tokenFrom">` + JS `pravocaptcha.execute`). Эта защита не снимается ни правильным `Referer`'ом через CDP, ни UA, ни прогретой сессией с куки. **Срабатывает только legitimate-flow**: open card → click on PDF link (= same JS-context, same opener).

Поэтому `KadSession.download_pdf(url, *, referer=card_url)`:
1. Если карточка дела (`referer`) ещё не открыта — `driver.get(referer)`
2. `window.open(pdf_url, '_blank')` — новая вкладка с тем же browsing-context, opener = карточка. kad доверяет такому запросу.
3. Chrome скачивает PDF на диск (Chrome prefs `plugins.always_open_pdf_externally=True` + `download.default_directory=/tmp/arbitr_dl_<uuid>/`, дублирован через CDP `Page.setDownloadBehavior`).
4. Ждём появления файла без `.crdownload` → читаем bytes → удаляем → закрываем «лишние» вкладки (в `finally`, чтоб подчистить и при captcha/error).

`_download_new_attachments(kad, case)` пробегает все `ArbitrAttachment.stored_file IS NULL` → `download_pdf(att.kad_url, referer=case.kad_url)` → `upload_file_to_s3(prefix='arbitr/<case_id>')` → `StoredFile` → `att.stored_file = stored`. Best-effort: ошибки отдельного файла не валят батч, capcha — пробрасываем (батч остановится, MAX-alert).

### IP-репутация kad

После 100-200 быстрых запросов с одного IP kad **жёстко повышает планку**: даже `/Card/<uuid>` начинает отдавать captcha. Уровень доверия откисает за несколько часов / суток. На dev-сервере (5.35.94.218) это подтверждено: после двух полных батчей parse_case + download_pdf эксперимент карточка стала возвращать 3.4КБ challenge вместо нормального 247КБ контента.

Стратегии:
- Не дёргать kad чаще `WORK_WINDOW` + 1 раз/час в авто-режиме.
- Ручные «Парсить сейчас» из UI — короткими сериями, не повторять одно и то же дело подряд.
- Для долгоиграющей prod-нагрузки имеет смысл residential-прокси (например через [proxy.bright-data.com](https://proxy.bright-data.com)) — opts `--proxy-server` в KadSession.

### Известные ограничения

- **PDF flow не оттестирован end-to-end** — архитектура через `window.open` + `Chrome download prefs` готова, на тестовом MONITORING-кейсе А12-33291/2024 парсер собрал 170 событий и 46 attachments за 79с, но IP попал в kad blacklist (см. выше) и финальный прогон с PDF не получилось сделать. Когда репутация откиснет — `docker exec siricrm-arbitr-runner-1 python manage.py pdf_diag --referer <card_url> <pdf_url>` должен вернуть `OK: ct='application/pdf' bytes=… magic=b'%PDF-'`.

### Deploy на prod (apps/arbitr)

После того как изменения арбитра попали в `feat/production-ready` и на prod пришёл `git pull`:

1. **Env-vars в `.env.prod`** — дописать вручную: `ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID=<chat_id_получателя_алёртов>` и `ARBITR_HEADLESS=true`. Без `MAX_BOT_TOKEN` (уже есть) уведомления о капче не уйдут — `send_captcha_alert` залогирует «skipped».
2. **Rebuild через DevOps-панель** (`https://siricrm.ru/devops/` → секция Деплой → кнопка `rebuild`). `Dockerfile` для `web/celery/devops-runner` не менялся — пересоберётся быстро; новый `arbitr-runner` (на базе `python:3.11-slim + google-chrome + chromedriver + Xvfb`) тянет ~1ГБ apt-пакетов, **первая сборка минут 5-7**. Альтернатива — вручную на prod:
   ```bash
   cd /var/www/projects/siricrm && git pull --ff-only
   ENV_FILE=.env.prod docker compose -f docker-compose.prod-host.yml --env-file .env.prod build arbitr-runner
   ENV_FILE=.env.prod docker compose -f docker-compose.prod-host.yml --env-file .env.prod up -d --no-deps arbitr-runner
   ENV_FILE=.env.prod docker compose -f docker-compose.prod-host.yml --env-file .env.prod up -d --force-recreate web
   ```
3. **Миграции** — `web` при старте сам сделает `migrate`. Применятся: `arbitr.0001`, `arbitr.0002` (search_hits/search_hits_at), `core.0016` (MenuItem «Арбитраж»), `core.0017` (привязка MenuItem к DashboardConfig).
4. **Рестарт `devops-runner` на prod** — обязательно после rebuild через панель (см. ниже про DevOps), иначе в воркере останется старый код без `arbitr.kad_monitor_one_case` и др. Так же — `arbitr-runner` мог не пересобраться если rebuild через панель его не задел, тогда:
   ```bash
   docker compose ... restart devops-runner arbitr-runner
   ```
5. **Smoke**: `docker exec <prod>-arbitr-runner-1 python manage.py kad_probe open` должен вернуть «OK: kad открыт».

## UI/UX: auth + idle UX

### Multi-tab logout
При закрытии **последней вкладки** SiriCRM шлёт POST `/accounts/logout/` через `sendBeacon` — чтобы сессия не висела. Логика в `static/js/multi-tab-logout.js`, подключён в `dashboard.html`, `arbitr/_layout.html`, `devops/_layout.html`. **Каждый layout обязан подключать этот скрипт** — иначе:
- Страница без heartbeat'а не появится в `localStorage.sirius_tabs` → при её закрытии счётчик «живых» вкладок неверный → ложный logout.
- ИЛИ закрытие такой страницы не отправит logout вообще.

> ⚠ **Гочча**: `beforeunload` срабатывает и на навигации по `<a href>` внутри сайта. Раньше переход `/dashboard/` → `/arbitr/` (любой full-page link) шёл с alive=0 (старая вкладка ещё не зарегистрировалась) → ложный sendBeacon('/accounts/logout/') → конкурентный GET /arbitr/ ловил `UpdateError: session was deleted` → 400. Лечение: при клике на `<a href>` того же origin или submit формы скрипт ставит `sessionStorage.sirius_internal_nav = Date.now()` — `beforeunload` видит метку (≤5 сек) и пропускает sendBeacon. Реальные закрытия (Alt+F4, кнопка ×) метку не ставят → logout уходит как раньше.

### Idle UX — warning + locked-overlay (без редиректа на /login/)

`IDLE_TIMEOUT_MINUTES = 5` (`config/settings/base.py`). Поток в `dashboard.html` (IIFE снизу) + `apps/core/views.py` + `apps/core/middleware.py`:

1. JS poller каждые **15с** → `GET /api/session/idle-check/` (этот путь в `IDLE_IGNORE_PREFIXES` middleware'а → НЕ обновляет `last_activity` и НЕ дёргает auto-logout сам).
2. Ответ: `{authenticated, idle_seconds, timeout_seconds, warning_seconds=60, logout_reason}`.
3. За **60с** до таймаута → **warning-модалка** (`#idle-warning`, z-index 9998) с countdown'ом и кнопкой «Остаться» (POST `/api/session/stay/` → обновляет `last_activity`).
4. После **300с** middleware (`IdleAutoLogoutMiddleware`) делает `auth_logout()` и кладёт `logout_reason` в сессию.
5. Следующий poll получает `authenticated=false` → **locked-overlay** (`#idle-locked`, z-index 9999) с inline-формой логина (POST `/api/session/login/` → `authenticate + login` в той же сессии, потом `window.location.reload()`).

**Ключевые механизмы (всё в dashboard.html IIFE):**
- `visibilitychange` + `focus` → `poll()` сразу. Без этого browser throttle'ит `setInterval` в фоновых вкладках до 1/мин → юзер мог вернуться и кликнуть до того как poll увидит logout.
- Конец warning-countdown → `poll()` сразу (а не ждать 15с).
- **`htmx:beforeOnLoad`** — перехват ответов с `HX-Redirect: /accounts/login/` или статусом `401`. Вместо HTMX-редиректа на login (через `HtmxLoginRedirectMiddleware`) показываем locked-overlay. Этим закрываем гонку «юзер кликнул до того как poll увидел logout» — первый же XHR-ответ показывает оверлей.
- **Capture-phase trap** для `click` / `keydown` / `submit`: пока `_showLocked === true`, всё что не внутри `#idle-locked` блокируется. Защита от `<a href>` навигации (которая идёт мимо HTMX) и от элементов с z-index ≥ 9999.

**Эндпоинты `/api/session/`:**
- `idle-check/` — публичный (auth не обязателен), в `IDLE_IGNORE_PREFIXES`.
- `stay/` — `@require_POST`, требует auth, обновляет `last_activity`. НЕ в IGNORE_PREFIXES.
- `login/` — `@require_POST`, JSON `{username, password}` → `authenticate + login`. НЕ в IGNORE_PREFIXES.

> ⚠ **Карточки клиента как отдельной страницы НЕТ** — в `apps/crm/urls.py` нет паттерна `clients/<uuid>/` без суффикса. Клиент открывается через `{% url 'chat' client.id %}` (`/clients/<uuid>/chat/`) HTMX-swap'ом в `#content-area` дашборда. Прямой переход на чат-URL даст голый партиал без sidebar. Для ссылок «открыть клиента» из вне дашборда (например, из `/arbitr/`) пока показываем ФИО просто текстом — когда понадобится deep-link, сделать обработчик `/dashboard/?openClient=<uuid>` в `dashboard.html`.

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

**Модалка лога** (`templates/crm/partials/client_events_modal.html`) — открывается через `GET /clients/<uuid>/events/` (с фильтрами `?kind=&source=&type=&q=`); добавление через `POST /clients/<uuid>/events/add/` (поля `entry_kind`, `type_code`, `comment`, `parent_id` + эхо фильтров). Размер фиксированный — `width:95vw; max-width:1600px; height:90vh`. Лента в стиле чата (старое сверху, новое снизу, auto-scrollTop = scrollHeight при открытии). Цветовое кодирование: события — тёмно-синий шрифт `#1e3a8a`, действия — тёмно-зелёный `#166534` (Tailwind blue-900 / green-800 inline, классы не нужны).

## Bubble-импорт (`apps/bubble_import/`)

- **Лимит Bubble Data API на cursor-пагинацию — 50 000** записей. Сущности с большим объёмом (`MessageWSP`, `Files`) дофетчиваются **окнами по 30 дней** через `services.fetch_window()`. См. `tasks.WINDOWED_ENTITIES` + `WINDOW_YEARS_BY_ENTITY`. Идемпотентно через `update_or_create(entity, bubble_id)` — повторный fetch не дублирует.
- **`ProjectBFL.telWSP`** = WhatsApp-номер клиента по конкретной услуге. `apply_projectbfl` пишет его в `ClientPhone(purpose='whatsapp')` алиас — тот же клиент может писать с нескольких номеров.
- **WA-медиа vs Files**: `StoredFile.bubble_id` имеет префикс `wamedia_<msg_id>` для медиа из чатов и чистый bubble id для документов из таблицы `Files` — пересечений в БД нет (могут быть в S3 как двойные ключи, безвредно).
- **Management-команды** (для дочистки после массового импорта):
  - `sync_projectbfl_aliases` — пройтись по уже импортированным ProjectBFL и заполнить `ClientPhone(whatsapp)` из `raw.telWSP`.
  - `reapply_failed_wa` — сбрасывает MessageWSP с ошибкой «клиент не найден» в `pending+approved` и прогоняет; полезно после sync алиасов.
  - `create_leads_from_failed_wa` — для оставшихся непривязанных номеров создаёт клиентов-лидов (как при онлайн-обращении) и сбрасывает их сообщения в pending для повторного apply.
- **Долгие job'ы**: `full_import_task` имеет `time_limit=24h`. Для длинных apply'ев (часы) запускай через `docker compose exec -d web sh -c "nohup python manage.py apply_bubble <Entity> > /tmp/x.log 2>&1 &"` — переживёт SSH-разрыв.

> ⚠ **`BubbleRecord.target_id` — это `str`, не `UUID`.** При маппинге на нашу модель (`Service.id`, `Client.id` — UUID) нельзя сравнивать в set'е напрямую: `target_id in service_ids` будет False даже при совпадающих значениях. Конвертировать: `uuid.UUID(str(br.target_id))`. Django ORM сам конвертирует в `filter(pk__in=[...])` (поэтому update'ы из такого set работают), но set-difference / dict-key lookup — тихо ломается. Прецедент — `backfill_status_from_bubble` (первый прогон проставил Service правильно, но Client.status весь оказался `unknown` из-за этого).

### Backfill StatusPrj (этапы услуг + статусы клиентов)
`python manage.py backfill_status_from_bubble [--dry-run]` (`apps/bubble_import/management/commands/`) — тянет `StatusPrj`-таблицу из Bubble (16 записей с `nameStatusPrj`), по жёстко прошитому `STATUS_MAP` в файле команды ставит `Service.common_status` всем 5764 услугам и пересчитывает `Client.status` по приоритету `PRIORITY = {active:1, closed:2, lead:3, unknown:4, refused:5, archive:6, to_delete:7}` (для каждого клиента — наивысший статус среди его услуг). Идемпотентна. Услуги без `statusPrj` / backing ProjectBFL → дефолт «Лидогенератор» (вклад в клиента — `unknown`). Клиенты без услуг — не трогаются.

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
