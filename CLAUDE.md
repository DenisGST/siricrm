# CLAUDE.md — SiriCRM

Этот файл автоматически загружается в контекст Claude Code в каждой сессии.
Держи его компактным и актуальным. Технические детали — в `docs/`, пользовательские инструкции — в `guides/`.

## Что за проект

CRM для юридической фирмы (банкротство физлиц / БФЛ). Django 5.2 + HTMX 1.9.8 + daisyUI 4 (Tailwind, pre-compiled) + Celery + Channels (WebSocket). Интеграции: Telegram (userbot на Telethon + бот), MaxChat, Beget S3 (медиа + бэкапы), DaData.

## Карта окружений

| Окружение | Сервер | Домен | nginx | compose-файл | env-файл |
| --------- | ------ | ----- | ----- | ------------ | -------- |
| **prod**  | 45.90.35.187 | siricrm.ru (+ www, flower., redis.) | системный (не Docker): SSL certbot, антисканеры | `docker-compose.prod-host.yml` | `.env.prod` |
| **dev**   | 213.155.29.31 | crmsiri.ru (+ www) | докеризованный (в стеке), антисканеры | `docker-compose.prod.yml` | `.env.dev` |

Разработка сейчас ведётся на **dev** (213.155.29.31, Алматы, AS49791 Newserverlife). Прежний dev `5.35.94.218` оставлен резервом до ~16.08.2026, затем гасить. Prod — боевой, не трогать без необходимости.

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

`ALLOWED_HOSTS` в `prod.py` приходит из env + дополнительно хардкодом `45.90.35.187` и `5.35.94.218` (внутренние запросы серверов к самим себе по IP). 🛑 Хардкод содержит **старый** dev-IP; актуальный `213.155.29.31` добавлен в `ALLOWED_HOSTS` внутри `.env.dev` — правка кода не потребовалась. При случае почистить хардкод. Sentry-фильтр `before_send` дропает `DisallowedHost`-инциденты от внешних сканеров.

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
apps/accounting     — рабочее место бухгалтера: разнесение входящих платежей (выписка ТБанк + эквайринг, см. ниже)
apps/procedure      — рабочее место помощника АУ: карточка дела о банкротстве (стадии/процедуры/сроки/данные должника, см. ниже) — Этап 1, на проде с 15.06.2026
apps/efrsb          — интеграция с ЕФРСБ (fedresurs.ru): генератор текстов сообщений (движок АФД) + read-API мониторинг публикаций; вкладка «Публикации» карточки дела (см. ниже)
apps/kommersant     — публикации в газете «Коммерсантъ»: текст сообщения, бланк заявки ИД, подача по email с ящика АУ, приём счёта по IMAP (см. ниже)
apps/reports        — раздел «Отчёты»: аналитика/отчётность (вкладки), 1-й отчёт «Отдел продаж» + бюджет ОП (см. ниже)
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

**Полное описание:** [`arbitr_integration_claude.md`](./arbitr_integration_claude.md) — антидетект-стек Selenium+Xvfb, прогрев сессии, селекторы, capture detection, скачивание PDF, IP-репутация, отладка через `kad_probe`, deploy на prod, **smart-parser архитектура (3 параллельных runner'а + per-IP SNAT-ротация + per-IP cooldown), UI-панель real-time, real-time IP-расписание.**

Кратко: Selenium+headed Chrome через Xvfb обходит anti-bot kad. **Три параллельных контейнера arbitr-runner (a/b/c), каждый со своей celery-очередью `arbitr_<id>`** — beat-таски `arbitr.kad_smart_one_<a/b/c>` каждые 10с дёргают `_kad_smart_one(runner_id)` → атомарный `SELECT FOR UPDATE SKIP LOCKED` забирает кейс → парсит → throttle 3-15 мин rnd при success/новом, 10с при «без нового», 30 мин каждые 8 успехов. **Outbound IP — host-side iptables SNAT по docker source-IP** (правила выставляет `ops/arbitr-snat-rotate.sh` через systemd-timer раз/мин). 4 IP по расписанию (МСК): `45.90.35.187` 21–5, `31.128.40.116` 5–15, `45.12.239.248` 9–17, `109.172.47.2` 11–20; на пике 11–15 активны 3 IP → 3 runner'а параллельно. **Per-IP captcha cooldown 12ч** — капча на одном IP останавливает только его, остальные продолжают (`cooldown.is_active(ip)/until(ip)/clear(ip)`); ручной сброс `python manage.py arbitr_clear_cooldown [--ip X.X.X.X]`. **Real-time панель на `/arbitr/`** (HTMX полит 5с): 3 runner-карточки (зелёный=парсит сейчас + name дела), статистика 24ч, готовых к парсингу, последние 5; нижняя строка — 4 IP-чипа с расписанием, активные подсвечены, в капче — красные. Алёрты в MAX: при капче «🚫 IP X в капче до HH:MM, парсинг через другие продолжается». **При НОВЫХ судебных записях — персональные уведомления сотрудникам в MAX** (см. «MAX: уведомления о судебных событиях» ниже); старый success-алёрт «✅ Скачано N,M» на каждый парсинг убран (флудил пустыми). SEARCHING → MONITORING через UI «Это моё дело» в `/arbitr/`. Полная документация по селекторам, ловушкам, и анти-детекту — в `arbitr_integration_claude.md`.

🛑 **На dev арбитраж не работает вовсе** (проверено 04.08.2026) — вся мульти-раннерная схема выше описывает **прод**. На dev: контейнер `arbitr-runner` один (не a/b/c/d), все три beat-задачи (`arbitr-kad-monitor-case` / `-pending` / `-throttled-one`) выключены, `last_checked_at` у дел пуст — парсинг не запускался ни разу (1202 дела в статусе `monitoring` пришли с прод-дампом). Второй IP `45.12.73.96` и правило SNAT на kad **удалены 04.08.2026** как неиспользуемые: счётчик правила показывал `0 pkts, 0 bytes`, а сам адрес не был прописан ни в netplan, ни в systemd-networkd (добавлен вручную и не пережил бы перезагрузку). Если понадобится тестировать парсер на dev — трафик к kad пойдёт с основного `5.35.94.218`, у которого нет наработанной репутации для DDoS-Guard. Скрипт и unit-файлы ротации в репо есть (`ops/arbitr-snat-rotate.{sh,service,timer}`), но на dev не установлены. На проде — 4 раннера (`arbitr-runner` + `-b`/`-c`/`-d`) и 5 публичных IP на `eth0`.

## UI/UX: auth + idle UX

Multi-tab logout (grace-версия, **без beacon на unload**); idle-таймаут 10 мин с warning/locked-overlay (без редиректа на /login/); keepalive активности через `GET /api/session/idle-check/?a=1`. **🛑 Инварианты keepalive хрупкие — НЕ ломать** (легко вернуть массовые ложные логауты). Полное описание + «не ломать»-блок + эндпоинты `/api/session/`: **[`docs/auth-idle-ux.md`](./docs/auth-idle-ux.md)**.
> ⚠ Карточки клиента как отдельной страницы НЕТ (`apps/crm/urls.py`) — открывается `{% url 'chat' client.id %}` HTMX-swap'ом в `#content-area`. Детали — `docs/ui-conventions.md`.

**Профиль сотрудника** (`/profile/`, клик по кружку в сайдбаре → своп в `#content-area`): ФИО/мобильный/пароль/аватар, 4 независимых блок-партиала (`templates/core/partials/_profile_*.html`). Свой правит каждый, **чужой — только `is_admin`** (кнопка «Профиль ↗» в модалке сотрудника); пароль свой — с текущим, чужой — `SetPasswordForm` без старого. 🛑 **Аватар в S3, не ImageField** (MEDIA_ROOT не смонтирован → пропал бы при рестарте): `Employee.avatar_key` (миграция `core/0031`), обработка Pillow (EXIF/квадрат/256×256 JPEG) в `apps/core/avatar.py`, ссылка `avatar_url` кэш в Redis. **Список сотрудников** (Панель управления) — фильтры (поиск по словам/отдел/роль) + сортировка + чек «Показывать уволенных» (деф. OFF); 🛑 sort/dir в URL, фильтры через `hx-include`, sort/dir в форме НЕ дублировать. Память: `employee-profile-page`. 🛑 Гочча: в собранном `tailwind.css` есть только `toggle-primary`/`toggle-sm` (`toggle-error`/`toggle-info` бесцветны).

## Переезд dev на новую площадку (09.08.2026)

dev переехал `5.35.94.218` → **`213.155.29.31`** (Алматы, AS49791 Newserverlife). Домен, стек и `.env.dev` те же.

**Зачем.** Пробная площадка `5.180.21.126` (AS200590 NLS Kazakhstan) оказалась непригодна: с неё **не было маршрутов до российских сетей ни в одну сторону** — таймаут на S3 Beget, прод, старый dev, MAX, ТБанк, kad. Причём ломался и обратный трафик: пакеты доходили до прода, прод отвечал, ответы не возвращались. Из-за этого не строился даже VPN-туннель (рукопожатие уходит, ответ не приходит). У AS49791 связность нормальная — 0% потерь до прода/S3, туннели и обходные пути не нужны.

🛑 **Дело было в провайдере, не в стране.** При выборе площадки проверять не только «пинг до РФ», но и обратное направление: `ssh` с прода на кандидата должен доходить хотя бы до аутентификации.

**Что перенесено:** база — **с прода** (`pg_dump` 234 МБ → restore 1 мин 41 с, 0 ошибок), `.env.dev`, конфиги `awg0/awg1` + `awg-failover`. Код — свежий `git clone`. Медиа не переносились: S3 внешний.

### 🛑 Заливка прод-базы на dev: обязательная «дев-настройка» после restore

Прод-дамп приносит **прод-состояние планировщика**. Без правки dev начнёт работать как второй прод:

```sql
-- 4 задачи парсера арбитража на проде ВКЛЮЧЕНЫ, на dev их быть не должно
UPDATE django_celery_beat_periodictask SET enabled=false WHERE name LIKE 'arbitr-kad-smart-one%';
```

Иначе dev каждые 10 секунд полезет на kad.arbitr.ru параллельно с боевым и сожжёт репутацию прод-IP.

Второе: у прода **пустая таблица `devops_environment`** (панель живёт на dev, прод только отдаёт агента). После restore обязательно `python manage.py devops_setup` — иначе DevOps-панель на dev пустая.

### 🛑 Файлы из прод-бакета на dev недоступны — это норма

После заливки прод-базы `StoredFile.bucket` у большинства записей = `1464bbae4a12-sirius-s3` (боевой), а у dev-ключей S3 прав на него нет → **403**. На старом dev ровно так же: из ~192k файлов 162k числятся в прод-бакете и не открываются, работают только ~30k загруженных на самом dev. Ожидаемое разделение, не поломка. Давать dev-ключу доступ к боевому бакету — решение осознанное и рискованное.

### 🛑 `.env.dev` должен переживать `source` (bash)

`scripts/init-letsencrypt.sh` делает `set -a; source "$ENV_FILE"`. Значение со скобками без кавычек роняет его:

```
HEALTH_MONITOR_LABEL=ПРОД (siricrm.ru)   → syntax error near unexpected token `('
HEALTH_MONITOR_LABEL="ПРОД (siricrm.ru)" → ок
```

Кавычки безопасны для всех трёх потребителей: bash снимает, python-decouple снимает, docker compose снимает. Проверено — Django читает значение без кавычек.

### Порядок переключения (отработан)

1. Подготовка без простоя: пакеты, Docker, AmneziaWG (PPA `ppa:amnezia/ppa`), `git clone`, перенос `.env.dev` и конфигов VPN, `ALLOWED_HOSTS` += новый IP, репетиция restore.
2. Свежий дамп прода → новый dev (маршрут прод → старый dev → новый: 15 секунд суммарно, быстрее чем через рабочую машину).
3. Дев-настройка базы (см. выше) + `devops_setup`.
4. Гасим старый dev: `stop web celery celery-beat userbot`, затем `systemctl stop awg-quick@awg1 awg-quick@awg0`.
5. Поднимаем VPN на новом: `systemctl enable --now awg-quick@awg0 awg-quick@awg1 awg-failover`.
6. **DNS** A-записи `crmsiri.ru` + `www` → новый IP.
7. Сертификат: сперва `LETSENCRYPT_STAGING=1`, затем `=0`.
8. `ENV_FILE=.env.dev docker compose -f docker-compose.prod.yml --env-file .env.dev up -d`.

🛑 **VPN-ключ нельзя держать на двух машинах** — поднимать на новом только после `stop` на старом.

🛑 **TTL DNS был 21600 (6 часов)** — снижать до 300 заранее, иначе часть клиентов ещё полдня идёт на старый сервер и получает 502 (там nginx жив, а приложение погашено).

🛑 `ENV_FILE=` перед `docker compose` **обязателен** — без него compose подставляет `.env.prod` из `env_file:` и падает «`.env.prod` not found». Та же ловушка, что с `exec`.

### SSH после переезда

Алиасы обновлены на рабочей машине и на проде: `siri-dev` → `213.155.29.31`, добавлен `siri-dev-old` → `5.35.94.218`. На новом dev заведён свой ключ (`/root/.ssh/id_ed25519`, комментарий `siri-dev-new`) и конфиг с `siri-prod`/`siri-dev-old`.

🛑 **Ключ нового dev в `authorized_keys` прода не добавлен** — `ssh siri-prod` с нового dev пока не работает. Деплой и мониторинг это не затрагивает: они идут по HTTPS через DevOps-агента с токеном.

🛑 `ssh-keyscan` c рабочей машины не снимает ключ старого dev (`unsupported KEX method sntrup761x25519-sha512`, OpenSSH 8.1 слишком старый) — использовать `ssh -o StrictHostKeyChecking=accept-new`.

## Инфраструктурные особенности

- **VPN (Amnezia/WireGuard)** на обоих серверах — split-tunnel: через VPN идёт только трафик к Telegram-подсетям (`149.154.160.0/20`, `91.108.4.0/22`…`91.108.56.0/22`) и Anthropic (`160.79.104.10/32`, `34.36.57.103/32`), остальное напрямую. 🛑 Через `awg-quick`/`wg-quick` **не ставить `AllowedIPs=0.0.0.0/0` без `Table=off`** — иначе уведёт дефолтный маршрут и порвёт SSH. (Основные каналы `awg1` на обеих машинах специально используют `AllowedIPs=0.0.0.0/0` + `Table=off` + ручной policy-routing — это безопасно, дефолт не трогается.)
  - **Основной peer с 03.08.2026 — `199.189.253.249:37100`** (AmneziaWG; один VPN-сервер на обе машины, но РАЗНЫЕ ключи и адреса): dev `awg1` = `10.8.1.3/32`, prod `awg1` = `10.8.1.4/32`. 🛑 **Один конфиг нельзя поставить на оба сервера**: PrivateKey определяет PublicKey, сервер видит один peer на ключ — два клиента начнут перехватывать сессию друг у друга (endpoint roaming) и связь порвётся на обеих. На каждый сервер — свой конфиг от провайдера.
  - 🛑 **Строку `DNS = ...` из сырого конфига провайдера НЕ переносить** — иначе `awg-quick` через resolvconf перепишет наш кастомный `/etc/resolv.conf` (`nameserver 198.18.18.18`).
  - 🛑 **Прод: адрес `10.8.1.4` дублируется** — его же держит резервный `claude0` (другой VPN-сервер, просто совпала подсеть), в таблице `local` две записи. Проверено 03.08.2026: failover НЕ ломается, оба туннеля пробиваются независимо (`curl --interface` использует `SO_BINDTODEVICE`, интерфейс задан жёстко). Латентная двусмысленность — при случае попросить провайдера перевыпустить прод-конфиг с другим адресом.
  - **История:** прежний основной peer `130.17.1.69:46161` (тоже общий для двух машин) **умер 31.07.2026 ~09:02** — обе машины потеряли handshake одновременно с точностью до минуты, что и указало на падение самого VPN-сервера. Failover отработал штатно (dev → `awg0`, prod → `claude0`), сервисы не пострадали. Бэкапы старых конфигов лежат рядом: `awg1.conf.bak-*`.
  - **dev**: основной `awg1` → `199.189.253.249:37100` (`10.8.1.3/32`); резерв `awg0` → `72.56.73.137:33886` (`10.8.1.32/32`, AmneziaWG, подсети прописаны прямо в его `AllowedIPs` → маршруты уходят в таблицу `main`). Конфиги — `/etc/amnezia/amneziawg/awg{0,1}.conf`.
  - **prod**: основной `awg1` → `199.189.253.249:37100` (`10.8.1.4/32`, `/etc/amnezia/amneziawg/awg1.conf`); резерв `claude0` → `72.56.73.137:37539` (`10.8.1.4/32`, плоский WireGuard, `/etc/wireguard/claude0.conf`, systemd `wg-quick@claude0`). На 03.08.2026 `claude0` живой — в июне 2026 он был мёртв 12+ часов и стоял вопрос «восстановить или снести»; хорошо, что не снесли: с 31.07 по 03.08 он в одиночку держал весь прод-трафик к Telegram/Anthropic.
  - **Маршрутизация основного канала** (одинаково на обеих машинах): `Table=off` + `MTU=1376` + MSS-clamp 1336 + PostUp/PostDown с `ip route replace default dev awg1 table 200` и 8 × `ip rule add to <подсеть> lookup 200 priority 100`. Приоритет `100` < `main` (32766) → перекрывает резерв; снять правила / `stop` сервиса → мгновенный откат на резервный канал.
  - **авто-failover**: `awg-failover.service` (`/usr/local/sbin/awg-failover.sh`); на dev `PRIMARY=awg1 BACKUP=awg0`, на prod `PRIMARY=awg1 BACKUP=claude0`. Каждые 15с пробивает ОБА туннеля напрямую (`curl --interface awgX` к Telegram/Anthropic) и переставляет `default` в таблице `200` на здоровый, предпочитая PRIMARY; если оба мертвы — остаётся на текущем (не переключается на дохлый). Гистерезис 2 цикла. Лог: `journalctl -t awg-failover`. Юнит `BindsTo`/`PartOf` = `awg-quick@awg1` → перезапускается вместе с ним.
  - **Замена peer'а (проверенный порядок)**: бэкап конфига → правим только `[Interface]` (Address/PrivateKey/Jc/Jmin/Jmax/S1/S2/H1–H4) и `[Peer]` (PublicKey/PresharedKey/Endpoint), блок PostUp/PostDown НЕ трогаем → `systemctl restart awg-quick@awg1`. Проверка: `awg show awg1` (свежий handshake), `ip route show table 200` (→ `dev awg1`), `curl --interface awg1 -o /dev/null -w '%{http_code}' https://api.telegram.org/` (ждём 302) и `https://api.anthropic.com/v1/messages` (405 = доступен). Дефолтный маршрут при `Table=off` не затрагивается — **SSH не рвётся**, а если новый peer окажется нерабочим, failover сам вернёт трафик на резерв за ~30с.
  - **Установка amneziawg-пакета** (нет в стандартных репах): `add-apt-repository -y ppa:amnezia/ppa && apt install -y amneziawg amneziawg-tools` — встанет userspace utility + DKMS-модуль ядра.
- **Telegram webhook'и не работают на наших серверах** — split-tunnel заворачивает ответный SYN-ACK на входящие от Telegram обратно в туннель, Telegram видит `Connection timed out`. Для приёма обновлений используем **polling** (`getUpdates`) через Celery beat — `apps/telegram/tasks.py:poll_telegram_leads` каждые 10с с long-poll timeout=20с и SETNX-локом `cache.add('telegram_leads:poll_lock')` (иначе параллельные таски ловят 409 Conflict). Чтобы выключить polling, не трогая код — `PeriodicTask.objects.filter(name='poll-telegram-leads').update(enabled=False)` (django-celery-beat хранит расписание в БД, beat подхватит за пару секунд).
- **S3 (Beget Cloud)**, endpoint `https://s3.ru1.storage.beget.cloud`, region `ru1`. Бакеты: prod media `1464bbae4a12-sirius-s3`, prod backups `1464bbae4a12-backup` (отдельные ключи `AWS_BACKUP_*`), dev media+backups `1464bbae4a12-siridev-s3`. **Beget валится на boto3 PUT (`XAmzContentSHA256Mismatch`)** — загрузка/скачивание только через pre-signed URL + `requests`. 🛑 **Файлы Bubble-импорта** лежат как `key=*.bin` + `content_type=octet-stream` + имя без расширения → inline-превью ломалось (качало `.bin` вместо показа). Фикс: `download_stored_file` (`?inline=1`) при неизвестном типе **снифает первые байты из S3** (`_sniff_stored_type`: PDF/JPEG/PNG/GIF), проставляет реальный `content_type` (и запоминает в `StoredFile`). Память `bubble-files-octet-stream`.
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

Кратко: тонкий слой `apps/maxchat/` (sender+tasks+views+processing, без своих моделей) над `Client/Message/StoredFile`. MAX Bot API **`https://platform-api2.max.ru`** (🛑 с 19.07.2026 MAX отключает старый `platform-api.max.ru`; URL в `settings.MAX_API_BASE_URL`, env-override есть). 🛑 **TLS нового хоста от CA Минцифры** (Russian Trusted Root CA) — нет в `certifi` → `CERTIFICATE_VERIFY_FAILED`. Решение: `certs/russian_trusted_ca.pem` (офиц. бандл gu-st.ru) + `apps/maxchat/ca.py:max_ca_bundle()` (certifi+рос.CA) передаётся в `verify=` ВСЕХ запросов sender'а и `_download_max_file`. auth header `Authorization: <MAX_BOT_TOKEN>` без Bearer. Отправка медиа: 3 шага (`POST /uploads` → `PUT <url>` → `POST /messages`). `Client.max_chat_id` обязателен. UI-кнопка `#btn-send-max` в чате. Также используется арбитром для алёртов о капче (`ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID`). 🛑 Приём вебхука вынесен в Celery (`processing.py:handle_max_event`, таска `process_incoming_max_event`) — как у WhatsApp, чтобы не вешать daphne. Id отправленного — в `message.body.mid` (не `message.id`).

🛑 **Дедуп входящих вложений — ПО ВЛОЖЕНИЮ, не по `mid`** (`processing.py`, фикс 17.06.2026). Несколько файлов/страниц в одном сообщении (договор альбомом фото) делят общий `mid` → старый дедуп по `mid` сохранял только первое, остальные страницы терялись («приходит 1-2 страницы»). Ключ вложения — `photo_id`/`token`/url → `raw_payload.att_uid`. Эхо своих исходящих ловим по `direction="outgoing"`. Скачивание — `_download_max_file` (ретраи×3 + проверка полноты по `Content-Length`, таймаут `(10,90)`). Входящие фото MAX отдаёт сжатыми в **webp** (`i.oneme.ru/i?r=<token>`).

🛑 **Входящие файлы НЕ теряем молча** (фикс 26.06.2026). Раньше при сбое скачивания был `continue` → файл пропадал без следа (жалобы Зинаида/Боголюбова). Теперь: (1) **download-fail** → запись «загружается» + Celery-автоповтор `retry_max_attachment_download` (бэкофф 1м→60м; URL живёт долго), по успеху файл появляется в чате, по исчерпанию — «не удалось, попросите переслать»; (2) **вложение без `url`** (ПЕРЕСЛАННЫЙ файл «Переслано: …» из др. мессенджера/почты — нестандартная структура) → видимый плейсхолдер + **полный payload в `raw_payload.body`** (durable-диагностика, переживает рестарт контейнера). 🛑 Доделать РЕАЛЬНОЕ скачивание пересланных: взять сэмпл `Message.objects.filter(channel="max", raw_payload__unhandled_attachment=True)` → разобрать `body` → добавить парсинг.

### MAX + Telegram: персональные уведомления о судебных событиях (парсер kad → сотрудник)

Парсер kad при **новых записях/файлах** дела шлёт непустое уведомление каждому сотруднику, подписанному на канал: **MAX** — `Employee.notify_court_events_max=True` + `max_chat_id`; **Telegram** — `Employee.notify_court_events_telegram=True` + `telegram_chat_id` (плюс `is_active=True`). Каналы независимы: можно включить оба (придёт в оба), ни одного — не придёт никуда. Пустые парсинги молчат. Формат одинаков — `[номер дела](kad-карточка) ФИО` / `Новая запись (дата): {тип акта}` / `{суть}` / `Новый файл: [name](kad-файл)` — отличается только разметка ссылок: MAX = markdown (`_MarkdownFmt`), Telegram = `parse_mode=HTML` (`_HtmlFmt`, экранирует `< > &`). Билдер — `apps/arbitr/notifications.py:_build_court_event_text(case, detail, fmt)`; рассылка — `send_court_event_notifications` → `_send_court_events_max` + `_send_court_events_telegram`; вызов из `_kad_smart_one` и ручного `kad_monitor_one_case` (идемпотентно — событие «новое» лишь при первом обнаружении). Детализация новых записей отдаётся из `_persist_case_info` (`new_events_detail`). Отправка в TG — `apps/telegram/bot_sender.py:send_bot_message` (сигнатура зеркалит `send_max_message`).

🛑 **Семантика полей `ArbitrEvent`** (парсер `_parse_event_node`): `kind`=`.case-type`=ТИП акта; `title`=`.case-subject`=**ФИО СУДЬИ/стороны** (НЕ описание!); `description`=`.additional-info`+результат=суть. В уведомлении берём `kind`+`description`, НЕ `title` (был баг: показывал судью вместо акта). 🛑 В тексте markdown-ссылки экранируем `[]`→`()` (имена файлов kad бывают «[Подписано] …»). 🛑 Лимит текста MAX — 4000 симв. (режем по границе строки). 🛑 **Уведомления шлёт `_kad_smart_one`, а он живёт в `arbitr-runner` a/b/c/d** — не в `web`/`celery`. Поэтому раннеры надо трогать вручную ДВАЖДЫ, deploy-handler их не покрывает: **новый код** → `restart arbitr-runner arbitr-runner-{b,c,d}`; **изменился env** (токены ботов, ключи) → **`up -d --force-recreate`** тех же четырёх (🛑 `restart` env НЕ перечитывает — обжигались 20.07.2026: MAX-уведомления шли, а TG молчали, потому что раннеры несли старый отозванный `TELEGRAM_BOT_TOKEN`, хотя в `web`/`celery` уже лежал новый). Пересоздавать раннеры безопасно: `ops/arbitr-snat-rotate.sh` резолвит docker-IP заново (systemd-timer раз/мин), но лучше сразу дёрнуть `systemctl start arbitr-snat-rotate.service`, чтобы не было минутного окна с чужим egress-IP.

**Привязка Telegram сотрудника — тот же bot + одноразовый код** (зеркало MAX-флоу): `Employee.telegram_chat_id` (BigInteger unique, поле было с `core/0023`, до этого не использовалось) + чек `notify_court_events_telegram` (миграция `core/0035`). Профиль `/profile/`: карточка «Уведомления в Telegram» (`_profile_telegram.html` + view `core:profile_telegram`) → «Получить код» → `apps/telegram/linkcode.py:issue()` (Redis, namespace `tg:linkcode:*`, 15 мин) → сотрудник шлёт код боту (`TELEGRAM_BOT_TOKEN` своего сервера) → поллер `_poll_once` видит `chat.type == "private"` → `_handle_private_message` → `claim()` → `_bind_employee_telegram()` сохраняет `chat_id` + подтверждение. Не код → короткая подсказка. Опц. env `TELEGRAM_BOT_LINK` — ссылка на бота в инструкции.

🛑 **Кто именно поллит бота — по серверам разное** (`getUpdates` на один токен из двух мест = 409 Conflict): `poll-telegram-leads` **выключен в beat на обоих серверах** (лид-канал не настроен, TG-лидов на проде 0), поэтому личку ловит отдельная beat-задача **`telegram.poll_bot_private`** (`poll-telegram-bot-private`, раз в 10с, только личка — лиды не создаёт). На **dev** тот же токен занят ботом мониторинга (`MONITOR_BOT_POLL=true`), поэтому `poll_bot_private` там сам уступает (`skipped: monitor_bot_owns_polling`), а код привязки ловит `_handle_monitor_update` (проверка кода — ДО allowlist'а, код сам себе пароль). Если понадобятся и лиды, и привязка одним поллером — включать только `poll-telegram-leads`: его `_poll_once` умеет обе ветки. 🛑 Бот не пишет первым — но привязка и есть первое сообщение боту, так что у привязанных диалог уже открыт.

🛑 **У dev и прода РАЗНЫЕ боты — это намеренно, не рассинхрон конфигов.** Прод — **@SiriusCRMBot** (`8866517557`, заведён 20.07.2026, `TELEGRAM_BOT_LINK=https://t.me/SiriusCRMBot`); dev — свой бот, он же бот мониторинга. Один токен на два сервера невозможен: `getUpdates` с двух машин = 409 + апдейты рвутся пополам (потерялись бы и коды привязки, и кнопки «Статус прода»/«Статистика за сутки»). Отправка (`sendMessage`) при этом НЕ конфликтует — конфликтует только приём.

🛑 **Токен бота может быть мёртв, и это тихо** — прежний прод-токен (`8446931203:…`) отдавал `getMe → 401 Unauthorized` (отозван в BotFather), из-за чего молчали и TG-алёрты мониторинга, и никто этого не замечал. Диагностика: `curl https://api.telegram.org/bot$TOKEN/getMe`. Поллер логирует 401 **раз в час** (throttle `telegram_bot:401_logged`), а не каждые 10с, — но и не чинит сам: нужен новый токен + **`up -d --force-recreate web celery celery-beat`** (🛑 `restart` env НЕ перечитывает).

🛑 **Дубль ключа в `.env*` — тихая мина**: `TELEGRAM_BOT_TOKEN`, дописанный вторым в `.env.dev` (как «положить рядом»), перебивает первый — dev увёл бы прод-бота при ближайшем пересоздании контейнеров. Чужие/справочные значения в env-файле держать только закомментированными; проверка — `grep -c '^TELEGRAM_BOT_TOKEN=' .env.dev` должен вернуть 1.

**Привязка MAX сотрудника — bot + одноразовый код**: `Employee.max_chat_id` (CharField unique, миграция `core/0034`; раньше max_chat_id был только у `Client`). Профиль `/profile/`: карточка «Уведомления в MAX» (`_profile_max.html` + view `core:profile_max`) → «Получить код» → `apps/maxchat/linkcode.py:issue()` кладёт 6-симв. код в Redis на 15 мин → сотрудник шлёт код нашему MAX-боту → webhook `handle_max_event` ПЕРЕД созданием `Client` зовёт `linkcode.claim(text)` → `_bind_employee_max()` сохраняет `user_id` в `max_chat_id` + подтверждение. Опц. env `MAX_BOT_LINK` — ссылка на бота в инструкции. Чек в профиле правит `ProfileForm.notify_court_events_max`.

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

🛑 **`Service.objects.visible_to` ДОЛЖЕН зеркалить `Client.objects.visible_to`** (менять синхронно, вместе с `rules.py` `view_service`/`view_client`): «видит всех клиентов» ⇒ «видит и все их услуги». Полный «видит всё»-набор для услуг = superuser/admin/head_dep (`is_references_access`) ∪ `managing_partner`/`accountant` ∪ `Employee.is_owner` ∪ отдел с `Department.sees_all_clients=True`; остальные — только `Service.employees` ∪ `services_allowed`. Было расхождение (02.07.2026): `Service.visible_to` не учитывал `sees_all_clients`/`is_owner`/`managing_partner` → карточка процедуры БФЛ (`Service.objects.visible_to`) отдавала 404 юристу отдела с `sees_all_clients`, хотя клиент был виден. **«Юридический отдел БФЛ» на проде — `sees_all_clients=True`** (как и «Отдел продаж БФЛ»).

Helper `apps.core.permissions.can_view_all_clients(user)` + шаблонный фильтр `{% load permissions_tags %}` `{{ user|can_view_all_clients }}` — единая точка проверки «может смотреть всю компанию». Использовано в чат-фильтре «Все» (видна только management + `sees_all_clients`) и в backend-защите `telegram_clients_list` (scope='all' для остальных принудительно режется до 'mine').

Фильтры чат-панели «Мои» / «Отдел» учитывают и `Client.employees`, и `Service.employees` (через `Q(...)|Q(...).distinct()`).

## UI/UX-конвенции

Канбан-колонка (`PAGE_SIZE=12`, intersect-подгрузка, `#id`-root), `#global-progress` (watchdog от «вечной» полосы), `client_edit` без reload, файловый менеджер (office/PDF inline-превью), глобальный поиск, `showToast()`, кэш статики (`STORAGES` обязателен), `{% comment %}` для multiline, чат-модалка (ленивый список, `_activeTelegramClientId`, `?pin_client_id`). Подробно: **[`docs/ui-conventions.md`](./docs/ui-conventions.md)**. Виджеты-счётчики дашборда + 🛑 гочча наследования `hx-target` (затирала `<body>`) — **[`docs/dashboard-widgets.md`](./docs/dashboard-widgets.md)**.

## Лог клиента: события + действия (`ClientLogEntry`) — СОБЫТИЙКА

Единый лог `ClientLogEntry` (kind `event`/`action`, справочники `EventType`/`ActionType`). Писать через `apps/crm/client_log.py` (`record_event`/`record_action`/`record_legacy`), **не** `ClientLogEntry` напрямую. Модалка `client_events_modal` (`GET /clients/<uuid>/events/`, add — `POST .../events/add/`). Подробно (модель, миграции 0070-0078, UI, доработки): **[`docs/crm-log-sobytiyka.md`](./docs/crm-log-sobytiyka.md)**.

## Уведомления — реал-тайм оповещения сотрудников (`apps/notifications`)

**Полное описание:** [`notifications.md`](./notifications.md) — модель, флаг `notifies` в справочниках, получатели «Мой канбан» (+гочча с OR-join), WS-пуш, панель/кнопки/дропдауны, snooze-beat, деплой.

Кратко: запись событийки с флагом `notifies` на её `EventType`/`ActionType` → по строке `Notification` каждому, кто работает с клиентом (союз ответственные ∪ исполнители услуг ∪ отдел этапа; адресно — через `recipients=`). Хук в `client_log.record_event/record_action` (`_maybe_notify`, анти-дубль `spawns_event`). Колокол в шапке (серый/0 → красный/N) → панель справа-сверху (вкладки Новые/В работе/Отложенные/Закрытые), кнопки-иконки Ознакомлен/Принять/Исполнил/Отклонить(с причиной)/Напомнить позже (пресеты + datetime); реакция → `services.respond` пишет действие в событийку. WS поверх группы `user_notifications_{user.id}`. Отложенные сами возвращаются в «Новые» beat-таском `notifications.revive_snoozed` (раз в минуту). 🛑 Флаги `notifies` per-DB (включать на dev И prod); при деплое рестартить `celery`+`celery-beat`. Telegram-дублирование — Stage C, не сделано.

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

🛑 **Bubble-баг — ключи с лидирующим пробелом**: у Man есть поля **` isMarried`** и **` spouse`** (с пробелом в начале имени!). `raw.get("isMarried")` даёт None у всех 6550 записей хотя 646 женатых. В applier'е использовать `v("key") or v(" key")`. Команды `sync_spouses`, `sync_man_gender` это учитывают; при добавлении нового Bubble-поля — обязательно `SELECT raw->>'fld', raw->>' fld'` обоих вариантов.

**Команды целевой досинхронизации** (без полного reapply):
- `sync_spouses` — Man.` isMarried`/` spouse` → Client.is_married + spouse (зеркалит связь A↔B; на проде 880 is_married, 431 пара)
- `sync_man_gender` — Man.Пол → Client.gender (--force перезаписывает)
- `sync_arbitr_cases` — ProjectBFL.numbDelo/linkKadArbitr → procedure.ArbitrCase (status=monitoring; на проде создано 1192 дела)
- `import_bubble_correspondence_to_requests --reapply` — Bubble Сorrespondence → procedure.Request + пары crm.Correspondence (out/in). См. [Запросы/ответы](#запросыответы-в-госорганы--реализация-детально).
- `map_gosorgan_to_legalentities` — Bubble Gosorgan → существующие LegalEntity по нормализованному name + fuzzy (ИФНС↔Инспекция ФНС, МРЭО↔МЭО). После заливки судов и ОСП покрытие 575 LE с `bubble_id` из 1862 Gosorgan (1190 unmatched — имена в Bubble отличаются от официальных, требуется token-set fuzzy для добивки).

## Реестр госорганов в `crm.LegalEntity` (для запросов из procedure)

Помимо банков/кредиторов в `LegalEntity` теперь живёт справочник госорганов — наполнен из официальных источников (НЕ из Bubble, там кривые имена/адреса).

| Подмножество | Источник | Кол-во | Идемпотентный ключ | Kind |
|---|---|---|---|---|
| ОСП ФССП | `opendata.fssp.gov.ru/7709576929-osp` CSV (ежемесячно) | 2 868 | `fssp_code` («{region}-{code}», напр. «34-1») | «ФССП» |
| Районные/городские СОЮ | GitHub `dataout-org/sudrfparser` JSON (RS-фильтр) | 2 066 | `court_code` («{NN}RS{NNNN}») | «Районный суд» |
| Мировые участки | DaData `/findById/court` перебором `{NN}MS{NNNN}` | 7 744 | `court_code` («{NN}MS{NNNN}») | «Мировой участок» |

**Команды** (`apps/crm/management/commands/`):
- `import_fssp_osp` (CSV + DaData `/clean/address` для нормализации) + `refill_fssp_osp` (добивка после квоты + регион из имени/префикса fssp_code)
- `import_district_courts` (GitHub seed + DaData `/suggest/court` для адресов; алиасы регионов **20→95 Чечня, 91→82 Крым, 88→24 Эвенк**)
- `import_magistrate_courts` (DaData findById перебором; стоп после 30 пустых ответов подряд; идемпотентно через DB-кэш `court_code`)

**Адреса нормализуются** DaData `/clean/address`. **Регион** определяется из CSV `region_code`, fallback — `cleaned.region_kladr_id` (первые 2 цифры), затем эвристика «по <регион>» в имени, затем префикс fssp_code (для не-98).

🛑 **DaData квота 10k запросов/день, общая на API_KEY** — парсим ТОЛЬКО на dev, потом переносим dev→prod через `serializers.serialize → dumpdata → scp → loaddata` (см. подробно в памяти `external-api-dev-then-sync`). Не повторять парсинг отдельно на проде — съест квоту в 2 раза без пользы.

**Миграции добавляли**: `LegalEntity.fssp_code` + `LegalEntity.court_code` (оба unique nullable), `Region 84` (Херсонская обл — не было в исходной таблице), LegalEntityKind «Районный суд», «Мировой участок».

**Дыры**: 7 регионов без мировых (новые субъекты ДНР/ЛНР/Херсон/Запорожье + малые АО), 35 СОЮ без адреса (DaData не нашла). Удмуртия в Region имеет дубль (number=18 и number=118) — баг исходной таблицы.

## АФД — автоматическое формирование документов (`apps/afd/`)

Генерация документов с автозаполнением из CRM. Реализовано: **договор юруслуг БФЛ** (плейсхолдеры в .docx-шаблоне) и **заявление о признании банкротом + приложения** (секционный конструктор). Кнопки «Составить договор» и «⚖ Сформировать заявление о банкротстве» в модалке услуги.

### Договор БФЛ

- **Модели:** `ExecutorOrg` (реквизиты Исполнителя — справочник, редактируется в UI; плейсхолдеры `{ispolnitel}`/`{Реквизиты_исполнителя}`/`{Исполнитель}`), `DocumentTemplate` (шаблон .docx в S3, `kind=contract_bfl`), `GeneratedDocument` (история генераций).
- **Движок** (`docx_engine.py:render_docx`) — заполняет `{плейсхолдеры}` **с учётом разбиения слова на runs** (Word дробит текст на `<w:r>`): склеивает run'ы абзаца, заменяет, пишет результат в первый run и очищает остальные. Обходит и таблицы, и колонтитулы.
- **PDF** (`pdf_utils.py:docx_to_pdf`) — `soffice --headless --convert-to pdf` (LibreOffice). **`libreoffice-writer` добавлен в `Dockerfile`** → ⚠ при выкатке на prod нужен **`rebuild`, не `deploy`** (и так из-за новых зависимостей `python-docx`, `pypdf` в requirements). Конвертация синхронная в `web` (не celery), ~3–10с.
- **Приложения к договору** склеиваются в итоговый PDF через `pypdf` (`pdf_utils.merge_pdfs`), порядок: договор → **согласие на обработку ПДн** (`appendix.consent_pdf`, reportlab; оператор — из `ExecutorOrg`: шапка=`{Реквизиты_исполнителя}`, тело=`{ispolnitel}`; данные клиента из CRM; ОГРН не печатается) → график платежей (`appendix.schedule_appendix_pdf`) → анкета (`questionnaire.pdf.generate_response_pdf`).
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

## Бухгалтерский учёт — рабочее место бухгалтера + ТБанк (`apps/accounting`)

**Полное описание:** [`accounting_integration_claude.md`](./accounting_integration_claude.md) — модели (`IncomingPayment`/`SourcePoll`/`AcquiringPrepay`), 3 вкладки UI, ручное разнесение (`services.py`), выписка р/с (T-API, поля операции, `is_settlement`), эквайринг (вебхук + подпись + prepay-эндпоинт), env, эндпоинты, гоччи, тест-сниппеты.

Кратко: раздел `/accounting/` (право `can_access_accounting` — роль `accountant`/admin/руководство). Входящие платежи из двух источников → очередь «Уведомления» (статус `не привязан`→`привязан`/`неопознанный`), бухгалтер вручную привязывает к клиенту и начислениям (разбивка суммы) → создаётся `finance.Payment`, гасится `Charge`. **Выписка р/с ТБанк** — поллинг `GET business.tbank.ru/openapi/api/v1/statement` (beat `accounting-poll-statement`, throttle 3ч); эквайринг-зачисления (плательщик «АО ТБанк», ИНН 7710140679) помечаются `is_settlement` и непривязываемы. **Эквайринг** — приём вебхуком `/accounting/acquiring/webhook/` (🛑 подпись: булево как `"true"/"false"`; ТБанк НЕ отдаёт введённые ФИО/телефон → берём из prepay `/accounting/acquiring/prepay/`, страница `fo-y.ru` шлёт их `sendBeacon`'ом по `OrderId`). 🛑 Деплой при смене env: deploy делает `restart` (env не перечитывает) → нужен `up --force-recreate`. 🛑 **Оплата с сайта — серверный Init, а НЕ JS-виджет ТБанка** (фикс 20.08.2026): виджет `securepay.tinkoff.ru/.../tinkoff_v2.js` отдаётся с сертификатом Russian Trusted CA (Минцифры) → в обычном браузере скрипт блокируется, `pay()` не определяется, кнопка «Оплатить» молча не работает (в CRM это видно как всплеск `AcquiringPrepay` при нуле платежей). Теперь форма fo-y.ru шлёт POST на `/accounting/acquiring/pay/` → `integrations.acquiring_init` (**`securepay.tbank.ru`**, Let's Encrypt) → `302` на `pay.tbank.ru`. Правка самой страницы сайта — `docs/foy-payment-form.html`. 🛑 **Токен T-API привязан к IP** — у прод-сервера 5 публичных IP, перезагрузка меняет primary egress → 403 `FORBIDDEN`; лечится перевыпуском токена с вайтлистом ВСЕХ прод-IP (или без ограничения). 🛑 **Роль `accountant`** = рабочее место бухгалтера: finance-CRUD (кроме удаления) + видит ВСЕХ клиентов/услуги (`visible_to`/`can_view_all_clients`/`rules.py` — менять синхронно). На dev поллинг выписки выключен. Боевая интеграция живая на проде (проверена реальным платежом). Подробно: файл выше + память `tbank-payments-integration-plan`.

## Процедуры банкротства — рабочее место помощника АУ (`apps/procedure`)

**Полное описание:** [`рабочее место помощника арбитражного управляющего.md`](<./рабочее место помощника арбитражного управляющего.md>) — домен БФЛ, двухуровневая модель (Дело→Процедуры), движок (`services.py`), карточка+вкладки, таймлайн в шапке, данные должника/супруга, даты/исходы, права, контроль сроков, сиды, деплой, гоччи, роадмап.

Кратко (Этап 1, **на dev и проде с 15.06.2026**): самый крупный раздел — веб-аналог «ПАУ». Карточка дела по услуге БФЛ, полноэкранный своп в `#content-area` из модалки услуги (кнопка «🗂 Карточка процедуры банкротства», право `can_access_procedures`). **Вход также пунктом меню «Юрист БФЛ»** (иконка-Фемида `static/icons/line/femida.svg`, секция «Инструменты», видимость по `can_access_procedures` в `context_processors`, сид меню — миграция `procedure/0008_seed_menu`) → лендинг `/procedure/` (пустая карточка); дело клиента открывается кнопкой **«⚖ Дело БФЛ»** в результатах глобального поиска и **иконкой-весами на карточке клиента главного канбана** (обе → `open_client_case`: 1 услуга → карточка, несколько → выбор, нет → подсказка). Кнопка на канбан-карточке видна только у клиентов с услугой БФЛ (`Client.has_bfl_service`, читает prefetch `services__name`) и при `can_access_procedures`. **Каталог мероприятий правится в Справочниках** (вкладка «Шаблоны мероприятий», `procedure:references_milestones`, гейт `is_references_access`) — не только в админке. **В шапке карточки (правый верхний угол) — иконка-кнопка «Файловый менеджер клиента»** (`files:manager client.id` → модалка на `body`; жёлтая `#facc15` на hover). **Запросы/ответы в госорганы + корреспонденция (Входящие/Исходящие/Судебные акты) + предпросмотр документов** (вкладка «Корреспонденция», на проде) — **реализация подробно в подразделе «Запросы/ответы в госорганы — реализация» ниже.** Кратко: реестр запросов (исх.№ сквозной по делу + срок ответа + контроль просрочки beat `procedure.mark_overdue_requests`); документ запроса **3 способами** (AFD по .docx-шаблону / подгрузка pdf-docx / онлайн-правка), документ → папка «Запросы» файл-менеджера; **ответ — 3 точки входа** (реестр / Входящие / модуль сканера), ставят `response_scan`+статус «ответ»; предпросмотр в модалке (PDF/сканы — iframe; doc/docx/xls — PDF-рендер через эндпоинт `office_pdf`, не MS Viewer — он не достаёт файл с Beget). Корреспонденция Входящие/Исходящие = записи `crm.Correspondence`, Судебные акты = вложения kad. Вкладка **Суд** (`_case_block.html` из `apps/arbitr`, управление доступно юристам/АУ по `can_access_procedures`, не только админам): нет дела → **«Включить мониторинг»** (+ раскрывашка «указать № дела и ссылку kad вручную» → сразу мониторинг, `arbitr:court_start`); идёт поиск/мониторинг → **«Найти/Парсить сейчас»** (прогресс-бар `arbitr:case_block_status` с кнопкой «⏹ Остановить» = `case_run_abort`), **«Остановить парсинг»** (пауза, `case_toggle_pause` с `stay=1` → возвращает блок), «Изменить № дела/ссылку»; пауза → **«Включить мониторинг»** (возобновление). 🛑 экшены arbitr, вызываемые вкладкой (`mark_iskotpravlen`/`confirm_case`/`case_run`/`case_run_abort`/`case_toggle_pause`/`case_confirm_hit`/`court_start`), гейтятся `_can_manage` = `is_admin ∪ can_access_procedures`. **Двухуровневая модель:** `BankruptcyCase` (1:1 к `crm.Service`) несёт общие стадии (Подготовка→Подача→Принятие/первое заседание) + итог 1-го заседания; дочерние `Procedure` (реструктуризация/реализация — их может быть несколько) со своими стадиями, датами (введение, **публикации ЕФРСБ и Коммерсантъ — две РАЗНЫЕ даты**), ФУ и исходом. Стадии/мероприятия-сроки — **данные** (`ProcedureStage`/`MilestoneTemplate`, DRAFT-сид, правит АУ), не хардкод. `ProcedureMilestone` — снапшот правила, `due_date = базовая дата + offset_days`; beat `procedure.mark_overdue_milestones` метит просрочку + событийка/уведомление. **Таймлайн стадий — в шапке карточки** (фазы: до введения / процедура(ы) / окончание; узлы шириной под название; авто-обновление через `HX-Trigger: procStagesChanged`). Данные должника/супруга правят карточку `Client` (телефоны/адреса CRUD scoped по `who`; супруг — выбор в 2 шага с явным «Сохранить»). Движок — чистые функции в `services.py` (`ensure_case` ленивый, `add_procedure`, `enter_stage`, `recompute_due_dates`, автозакрытие по терминальным исходам). 🛑 `@never_cache` на GET-партиалах; daisyUI timeline/collapse вырезаны (свой CSS/JS); ФУ — на процедуре. **На проде с 15.06.2026** (DRAFT-каталог мероприятий — подтвердить с АУ); запросы/корреспонденция/предпросмотр выкачены 21.06.2026. 🛑 `procedure_seed` и `load_request_templates` НЕ входят в deploy-handler — гонять вручную при выкатке (load_request_templates нужны .docx из `OLD/Шаблоны запросов/` — закоммичены в репо). Память: `procedure-module`, `strict-direction-procedure`.

### Запросы/ответы в госорганы — реализация (детально)

Вкладка «Корреспонденция» карточки дела. Поток: юрист формирует исходящий запрос в госорган → отправляет → ждёт и фиксирует ответ. Всё привязано к делу (`BankruptcyCase`), документы — к файл-менеджеру клиента.

**Модели** (`apps/procedure/models.py`):
- `Request` — FK `case`; `request_type`(FK)/`title`; `recipient`(FK `crm.LegalEntity`)/`recipient_name`; **`outgoing_number`** (сквозной исх.№ по делу); `status` `draft→sent→answered`/`no_answer`; отправка: `sent_method`/`sent_date`/`response_days`/`due_date`/`overdue_notified`; **ответ**: `response_date`/`response_number`/`response_text`/**`response_scan`**(FK `files.StoredFile`); **документ**: **`document_pdf`**/**`document_docx`**(FK `StoredFile`); `with_signature`/`generated_at`/`created_by`. Свойства `is_overdue`/`recipient_display`.
- `RequestType` (code/name/`default_recipient`/`response_days`/**`template`**→`afd.DocumentTemplate kind=request`/order/is_draft), `RequestPackage` (M2M типов — пакетное создание), `ArbitrationManager` (справочник АУ: ФИО/ИНН/СНИЛС/адрес/контакты/СРО/employee/**`signature_file`** — ОДИН PNG подпись+печать).

**Исх. № + дата создания**: `outgoing_number` присваивается СРАЗУ при создании (`services.create_request`, `max+1` по делу в транзакции; fallback и в генерации). Реестр-таблица показывает столбцы **«№»** (`outgoing_number`) и **«Создан»** (`created_at`). Срок ответа считается от даты отправки (`sent_date + response_days`); просрочку метит beat `procedure.mark_overdue_requests` → событийка `request_overdue`.

**Документ запроса — 3 варианта** (`apps/procedure/request_documents.py`; в колонке «Запрос» иконки-действия `.req-act`: серые `#94a3b8` → цветные на hover):
- 📄 **Автоформирование AFD** (`generate_request_document`): `build_request_context` собирает плейсхолдеры (арбитражный суд/№ дела — из `service.arbitr_case`; реквизиты АУ — из `ArbitrationManager` процедуры; данные супруга + свидетельство о браке — вводятся в форме генерации) → `render_docx(template_bytes, ctx)` (шаблон .docx типа качается из S3) → опц. `_apply_signature` (вставка PNG в абзац «Финансовый управляющий») → `docx_to_pdf` (LibreOffice). 🛑 Перед генерацией — `check_request_data` → **модалка предпроверки** (зелёным что есть, красным чего нет, кнопки «Всё равно продолжить»/«Отмена»).
- 📎 **Подгрузка готового** (`request_upload`, модалка `_request_upload_doc_modal.html`): `.pdf` → `document_pdf`; `.docx` → `document_docx` + авто-PDF (`docx_to_pdf`) в `document_pdf`.
- ✎ **Онлайн-правка** (`request_edit_save`): текст по абзацам (`extract_editable_paragraphs`/`apply_paragraph_edits`, форматирование/подпись сохраняются) → пересборка PDF.

**🛑 Связь с файлами клиента**: документ запроса (PDF **и** docx) ВСЕГДА подшивается в файл-менеджер клиента в папку **«Запросы»** (slug `requests`; `_attach`/`_store`, S3 prefix `procedure/requests`) + событийка `record_action('request_document_created', stored_file=pdf)`. Ссылки PDF/docx и в реестре запроса (`document_pdf`/`document_docx`).

**Ответ на запрос — 3 точки входа** (все ставят `response_scan` + `status='answered'` + `response_date`):
- 📥 **в реестре** (`request_response`): модалка с датой/№/текстом + скан (`_scan_to_storedfile`, S3 prefix `procedure/correspondence`). 🛑 этот путь кладёт файл ТОЛЬКО в `Request.response_scan` (S3, просмотр через `stored_download`) — в папку файл-менеджера НЕ подшивает.
- ☑ **в загрузке «Входящие»** (`correspondence_upload`, галочка «Это ответ на запрос» + выбор запроса): создаёт запись `crm.Correspondence(direction=incoming)` И тот же файл привязывает запросу как `response_scan` (`№`/дата ответа — из формы).
- 🖨 **в модуле сканера** (`apps/scans` — `client_targets`/`assign`, галочка «Это ответ на запрос в госорган (БФЛ)» + выбор запроса + дата): скан кладётся в ВЫБРАННУЮ папку файл-менеджера (создаётся `ClientFile`) И привязывается запросу как `response_scan` (+ опц. `Correspondence`, если отмечено). Список — запросы клиента в статусе `sent`/`no_answer`.

**🛑 «Входящие»/«Исходящие»/«Судебные акты» — это РАЗДЕЛЫ-аккордеоны вкладки, НЕ папки файл-менеджера:**
- Входящие/Исходящие = записи **`crm.Correspondence`** по `direction` (файл — `file_link` на скан в S3, prefix `procedure/correspondence`). Загрузка через `correspondence_upload` создаёт `Correspondence`, но `ClientFile` НЕ создаёт (исключение — путь через модуль сканера, где ClientFile делает сам сканер). Загрузка скана: контрагент — typeahead по реестру `LegalEntity` с фильтром по типу ЮЛ и галочкой «по региону дела».
- Судебные акты = `ArbitrAttachment` из дела kad (`apps/arbitr`) — скачанные вложения судебного дела.
- Единственная папка файл-менеджера в этом потоке — **«Запросы»** (документы запроса).

**Шаблоны/типы запросов** — команда `python manage.py load_request_templates` (идемпотентно, `--force` для перезалива): 11 `.docx` из **`OLD/Шаблоны запросов/`** (закоммичены в репо) → S3 (prefix `afd/request_templates`) + создаёт `RequestType` (code из MAP) + `DocumentTemplate(kind=request)`, привязывает шаблон к типу. Правка типов/пакетов/АУ — в Справочниках (`procedure:references_request_types`/`_request_packages`/`_managers`, гейт `is_references_access`). 🛑 На проде **справочник АУ пуст** — без `ArbitrationManager` с PNG-подписью AFD-генерация даёт пустой блок ФУ (предпроверка подсветит красным).

**Предпросмотр в модалке** (не новая вкладка браузера; 🛑 `procPreview`/`procPreviewOffice` определены **глобально в `dashboard.html`**, НЕ в HTMX-партиале — инлайн-скрипт в `innerHTML`-свопе вкладки мог не выполниться, и onclick падал на дефолтный `href` = скачивание; после правки docx «уходил в офис», PDF скачивался): `procPreview(url, name)` — `<dialog>` с iframe (PDF/сканы/картинки — inline через `stored_download?inline=1`). 🛑 **doc/docx/xls показываем как PDF-рендер, НЕ через MS Office Online Viewer** — `view.officeapps.live.com` не работает: Microsoft скачивает файл со своих серверов, а наш S3 — Beget (`s3.ru1.storage.beget.cloud`, российское облако) снаружи для них недоступен (pre-signed URL рабочий — проверено GET 200 — но Microsoft до Beget не достучаться). Эндпоинт **`procedure:office_pdf/<sf_id>/`** отдаёт PDF: если у документа запроса есть готовый PDF-двойник (`document_pdf`) — редирект на него; иначе docx/xls конвертируется на лету (LibreOffice `docx_to_pdf`). docx-ссылка → `office_pdf` → PDF в iframe; PDF/сканы/суд.акты → inline iframe; внешние/🔒 kad-ссылки → новая вкладка. (Office-превью файлового менеджера `templates/files/partials/preview.html` всё ещё использует MS Viewer — у него та же проблема с Beget, не трогали.)

### Почта России — выгрузка заказных писем в ЛК «Отправка» (этап 1)

Кнопка **«Выгрузка для Почты»** в панели таблицы запросов (вкладка Корреспонденция → Запросы, рядом с «Удалить выбранные»; гаснет при пустом выборе). По отмеченным чекбоксами запросам собирается **.xlsx в формате официального файла загрузки заказов** `otpravka.pochta.ru` → юрист грузит его в ЛК, **конверты со ШПИ и форму 103 печатает уже сам ЛК**.

🛑 **Форма 103 ≠ файл загрузки.** Мы генерируем ТОЛЬКО файл загрузки заказов; Ф103 («Список внутренних почтовых отправлений») печатается в ЛК после сборки партии — иначе она не совпадёт с тем, что Почта видит у себя, и в ОПС партию не примут. Свой генератор конвертов (`afd/envelope.py`) оставлен — он для «нарочно»/курьера, для почтовой пачки конверты даёт ЛК.

- **Модуль** — `apps/procedure/pochta_export.py`. 🛑 `COLUMNS` — 41 колонка **в точном порядке официального шаблона**, ЛК разбирает файл по позициям; шапка сверена побайтно с рабочим образцом юриста. Имя листа — `Лист`. Заполняем: `ADDRESSLINE` (индекс + адрес), `ADRESAT`, `MASS` (кг), `MAILTYPE=2` (письмо), `MAILCATEGORY=1` (заказное), `ORDERNUM` (`Исх-N`), `COMMENT` (тип запроса), `INDEXFROM`; при уведомлении — `NOTIFICATIONTYPE=S` (простое) + `NOTICEPAYMENTMETHOD=C` (безнал). 🛑 `ENVELOPETYPE`/`PAYMENTMETHOD` намеренно ПУСТЫЕ — берутся из настроек ЛК.
- **Состав выгрузки — не только запросы**: `Request` (госорганы) + опц. **кредиторы дела** (`recipients_for_case_creditors`) + **сам должник**. Реальная пачка АУ — все трое сразу.
- **Модалка предпросмотра** (`_pochta_export_modal.html`, view `requests_pochta_modal`) — единый список: годные и проблемные вперемешку, у проблемных красная подсветка и причина (нет наименования / адреса / индекса / перевес). **Вес и галочка «уведомление» правятся ПОСТРОЧНО** (в одной партии бывают и обычные заказные, и с уведомлением). Ключ строки — `req:<uuid>` / `cred:<i>` / `debtor`, из формы приходит как `mass:<key>` и `notify_keys`. Смена веса/состава пересобирает модалку (hx-post на себя), галочки уведомления — нет.
- **Вес**: `10 г конверт + 5 г × страниц`, предел письма **100 г** (выше — бандероль, строка отсеивается с подсказкой). Число страниц — поле **`Request.pages_count`**, заполняется при формировании AFD / онлайн-правке / подгрузке готового документа (хелпер `afd/pdf_utils.pdf_page_count`). 🛑 Так сделано, чтобы выгрузка **не ходила в S3** — на 1000 писем это были бы минуты в ASGI (грабля WA-вебхука).
- **`INDEXFROM`** — новое поле **`ArbitrationManager.ops_index`** (у каждого АУ своё ОПС), правится в Справочниках → Арбитражные управляющие; фолбэк — индекс из `corr_address`. Берётся от ФУ ТЕКУЩЕЙ процедуры дела: не назначен ФУ → колонка пустая.
- Адресат/отправитель переиспользуют `party_from_request` / `party_from_client` из `afd/envelope.py` — своего резолвера адреса нет.
- Скачивание — обычный GET (`requests_pochta_export`), HTMX файлы качать не умеет; кириллица в имени — через `filename*=UTF-8''` (RFC 5987). Чистка `\xa0` обязательна: в исходных адресах полно неразрывных пробелов от копипаста.
- Миграция `procedure/0022`. Новых зависимостей нет (`openpyxl` уже был) → выкатка обычным `deploy`. **На проде с 20.07.2026.**
- 🛑 У запросов из **Bubble-импорта** адресат записан текстом без связи с `LegalEntity` → нет адреса/индекса → массово отсеиваются (на тестовом деле 44 из 50). Это данные, не баг.

**Дальше по плану (не сделано):** вариант А — Otpravka API (`otpravka-api.pochta.ru`: `/1.0/clean/address`, `/1.0/user/backlog`, `/1.0/user/shipment`, печатные формы `/f7p`, `/f103`) + Tracking API (SOAP, отдельные креды) для факта вручения; вариант Б — ЭЗП (`/1.0/erl/send`, требует УКЭП и подписи `.sig` на каждое письмо). 🛑 У Почты **три разных договора/авторизации** — Otpravka, Tracking, ЭЗП идут отдельно. Договор — на АУ.

### Активы должника — вкладка «Активы» + распознавание справки ФНС

Вкладка **«Активы»** карточки дела (вместо бывшей заглушки «Документы»). Юрист кидает в дропзону («Распознать файл») PDF-пакет ответа ФНС по запросу АУ → парсер вытаскивает счета, имущество, доходы → модалка показывает **живой лог разбора** (поллинг 600 мс) и сводку → «Сохранить» раскладывает по разделам: **расчётные счета · 2-НДФЛ · недвижимость · земельные участки · транспорт · иное** (налоговая задолженность, участие в ЮЛ, адм. правонарушения). Разделы наполняются и по должнику, и по **супругу** (ФНС отвечает и по нему; в записях поле `subject`).

- **Модели** (`apps/procedure/models.py`): `AssetDocument` (источник: файл в S3 + сырой JSON разбора + субъект/дело/налоговый орган), `BankAccount` (+ `legal_entity` — матч банка по ИНН в реестре `crm.LegalEntity`, адресат будущего запроса выписки; `statement_request` — ссылка на запрос), `IncomeCertificate`, `RealEstateObject`, `LandPlot`, `Vehicle`, `OtherAsset`. Удаление `AssetDocument` каскадом сносит распознанное из него.
- **Парсер** — `apps/procedure/fns_parser.py` (`parse_stream` = генератор шагов лога, `parse_text` для тестов). 🛑 **Счета и имущество — из ТАБЛИЧНОГО слоя** (`pdfplumber.extract_tables`): в текстовом слое фрагменты многострочных ячеек («прекращено / право / использовани / я» и «ЭСП, не являющееся / корпоративным») перемешиваются между собой и оказываются ДО строки счёта — собрать их обратно из текста нельзя. Таблица отдаёт ячейку целиком (и шапку банка — одной ячейкой). Текстовый разбор счетов оставлен фолбэком (если таблицы не читаются) и покрыт тестами. 2-НДФЛ и шапка — из текста.
- 🛑 **Ловушки реальных справок** (проверено на 8 файлах разных УФНС в `OLD/ФНС/`; тесты `apps/procedure/tests/` + фикстуры-тексты): блок банка рвётся границей страницы (шапка не повторяется) → сквозной поток, не постранично; субъект может быть **супругом**, а не должником; **полной секции счетов может не быть — только Форма 9ф** (тогда все счета открыты); секции бывают продублированы в одном файле (→ `_dedupe` по номеру счёта / кадастровому № / VIN); номера ЭСП не 20-значные (`24A-0265256652`, `203422738`, `00000000200004309644`); у закрытого счёта «Вид счёта» пуст; **значение может стоять ПЕРЕД подписью-лейблом** — «Налоговый агент» и «Сведения сформированы» это подписи левой колонки, длинное имя обтекает их сверху и снизу.
- **Разбор — в Celery** (`procedure.parse_fns_document`), не в web: PDF-парсинг это секунды CPU, а тяжёлая работа в ASGI уже вешала daphne (инцидент WA-вебхука). Файл и лог живут в Redis 30 мин (`procedure:fns:<token>`), «Сохранить» подшивает исходник в S3 (prefix `procedure/assets`).
- **Повторная загрузка идемпотентна** — upsert по (дело + субъект + номер счёта / кадастровый № / VIN / год+агент 2-НДФЛ), дублей не будет.
- **Проверка на реальных PDF:** `python manage.py fns_parse <файл.pdf>` — печатает лог и распознанное, ничего не сохраняет.
- 🛑 Деплой: добавлена зависимость **`pdfplumber`** → на прод только **`rebuild`, не `deploy`**; после — рестарт `web` + `celery`.

## ЕФРСБ — публикации в Федресурс (`apps/efrsb`)

Тонкий слой над `apps/procedure`. Вкладка **«Публикации»** в карточке дела БФЛ → под-вкладки **ЕФРСБ** (`apps/efrsb`) и **КоммерсантЪ** (`apps/kommersant`, см. раздел ниже). **На проде**, каталог засеян. Генерация текста и копирование работают **уже сейчас**; read-API часть (проверка публикации, мониторинг) **СПЯЩАЯ** — `EFRSB_ENABLED=False`, кредов нет → `is_configured()=False`, кнопки проверки/мониторинга скрыты, beat `disabled`.

🛑 **У fedresurs/Интерфакс НЕТ публикационного API для третьих лиц.** Проверено по `fedresurs.ru/help#bankrupt` (доки в `OLD/EFRSB/`): и SOAP, и REST сервисы оператора — **только ЧТЕНИЕ** (памятка `Connection_info.pdf`; актуальная read-спека `Service_rest_1.4.0.pdf` добавляет только методы чтения). 🛑 **Импорт сообщения через XML тоже невозможен**: в руководстве ЛК АУ (`au_guide.pdf`) нет ни «импорта», ни «xml», ни «из файла» — сообщение создаётся **заполнением веб-формы** и подписывается УКЭП; XML-схемы (`PublicationsStructure.pdf`/`Messages_Structure.pdf`) описывают лишь **формат контента**, а не канал подачи. Машинно публикуют только крупные операторы (АСВ/ЦБ/ФНС/операторы СРО — см. `PublisherType` в схеме). **Единственный путь: генерируем текст → АУ вставляет его в ЛК вручную → мониторим факт/сроки через read-API.** Шов `submission.submit_publication` (`raise SubmissionNotAvailable`) + статус `submitted` оставлены на случай, если подачу когда-нибудь откроют.

### Каталог типов и подтипов (из официального справочника)

- **Типы** — полный официальный перечень: **119** (113 сообщений + 6 отчётов ФУ), 90 активных. Снапшот — `apps/efrsb/reference_data/message_types.json` (метод `v1/reference-books/message-types`). Ключ = **официальный код ЕФРСБ**, `api_type == code`; старые версии (`Meeting` vs `Meeting2`) — отдельными строками с `is_active`.
- **Подтипы** (`EfrsbMessageSubtype`) — **единственный официальный справочник подтипов = «типы судебных актов»** (`courtDecisionType`, **27**, из них 24 активных), применим **ТОЛЬКО к `ArbitralDecree`** («Сообщение о судебном акте»). Снапшот — `reference_data/court_decision_types.json`. У остальных типов подтипов нет (перечисления вроде формы торгов — это поля внутри формы, не подтипы).
- **Группировка** — официальное дерево категорий ЛК (`reference_data/categories.json`, 14 групп). Селектор в UI трёхуровневый: **категория → тип → подтип**; типы вне дерева попадают в «Прочее».
- **`is_bfl`** — разметка релевантности физлицам (35 типов, 18 подтипов). По умолчанию в селекторе видны только они; галочка **«Показать все»** открывает весь перечень. `applicable_kinds` (`restructuring`/`realization`) дополнительно фильтрует **по виду текущей процедуры** (напр. «введение реализации» не показывается в реструктуризации). 🛑 Разметка — **DRAFT, подтверждать с АУ** (правится в админке/Справочниках).
- Обновить снапшоты справочников: `python manage.py efrsb_pull_reference --contour demo` — гонять **на dev**, JSON коммитить (на проде сеть к ЕФРСБ не дёргаем).

### Текст сообщения — главный артефакт

- **Шаблон текста** — поле `text_template` на типе и на подтипе (**шаблон подтипа приоритетнее**). Плейсхолдеры `{…}` из `generator.build_context`: должник, ФУ, **СРО с ИНН/ОГРН/адресом**, суд, **адрес суда** (из `Region.court_address`), № дела, дата решения, срок процедуры, дата след. заседания. Засеяны образцы для судактов **`PropertySale`** (введение реализации) и **`DebtRestructuring`** (введение реструктуризации); остальные АУ заводит сам.
- **Поток добавления** (`views.publication_add` / `publication_gen_text` / `publication_save`): кнопка «+ Добавить сообщение» **неактивна, пока не выбран тип/подтип** → модалка: **вверху только НЕзаполненные/невалидные данные** (`generator.check_problems` — проверка строится по плейсхолдерам КОНКРЕТНОГО шаблона, а не по фиксированному списку) → кнопка «⚙ Сгенерировать текст» → **редактируемое поле с текстом** → **Сохранить / Отмена**. 🛑 **Запись создаётся только по «Сохранить»** (Отмена не оставляет мусора); тогда же из текста собираются .docx/.pdf → папка клиента «Публикации ЕФРСБ» + событийка `efrsb_text_generated`.
- **Копирование**: модалка текста с кнопкой «📋 Копировать» (иконка в реестре) — АУ вставляет текст в ЛК fedresurs. Правка текста — иконка ✏️ (та же модалка).

### Read-API: проверка публикации и мониторинг

- **Клиент** (`client.py`): чистый REST на `requests`, JWT в Redis (`efrsb:jwt`, 7.5ч), throttle ≤8 req/s, 401→релогин, 429→retry, demo/prod по `EFRSB_CONTOUR`. 🛑 **`datePublishBegin` и `datePublishEnd` ЕФРСБ требует только ПАРОЙ** (иначе 400, код 1000); ограничение «31 день» не действует, если задан `bankruptGUID`.
- **Точечная проверка** — `services.check_publication(pub)` + кнопка 🔍 «Проверить в ЕФРСБ» в реестре: ищет по `bankruptGuid` + типу, для судакта дополнительно **по подтипу через официальный фильтр `courtDecisionType`** (точное попадание). Нашла → привязывает `fedresurs_guid`/номер/дату/ссылку/`hasViolation`, статус → `published`, зовёт `apply_publication_date` + `flag_violation`. Защита от ложных срабатываний: окно «от создания заготовки −7 дней», исключаются guid'ы, уже привязанные к другим публикациям дела.
- **Фоновый мониторинг** (`tasks.py`, дефолтная celery-очередь — **БЕЗ роутинга**, браузер не нужен): `resolve_bankrupt_guid` (ИНН/СНИЛС→`bankruptGuid`, мульти-хит→ручной выбор), `sync_case` (→`upsert_publication`, дедуп по `fedresurs_guid`), `match_to_internal`, `apply_publication_date` (типы с `sets_efrsb_date=True` → ставят `Procedure.publication_efrsb_date` + `recompute_due_dates`), `flag_violation`. Beat `efrsb.monitor_active_cases` (раз в 4ч, гейт `EFRSB_MONITOR_ENABLED`, throttle `next_sync_at`).
- **Контроль сроков НЕ дублируем**: мониторинг лишь кормит дату-якорь `publication_efrsb_date`; просрочки метит существующий `procedure.mark_overdue_milestones`. `hasViolation` — постфактум-сигнал реестра (бейдж + разовая событийка).

### Модели, сид, деплой

- **Модели**: `EfrsbMessageType` (код=официальный; `api_kind` message/report, `applicable_kinds`, `is_bfl`, `sets_efrsb_date`, `text_template`), `EfrsbMessageSubtype` (FK на тип; `code`=`courtDecisionType`, `applicable_kinds`, `is_bfl`, `text_template`), `EfrsbBankruptLink` (1:1 к делу; кэш `bankrupt_guid` + candidates + throttle), `EfrsbPublication` (единый реестр: наши `internal` + обнаруженные `discovered`, поле `subtype`; дедуп по `fedresurs_guid`), `EfrsbPublicationFile`.
- 🛑 **Сид `efrsb_seed` НЕ в deploy-handler — гонять вручную** (как `procedure_seed`): грузит каталог из `reference_data/*.json` + подтипы + типы лога. Идемпотентен и **НЕ затирает правки АУ** (шаблоны / `is_bfl` / `applicable_kinds`). Перезалить текстовые шаблоны из кода — **`efrsb_seed --force-templates`**.
- Деплой: миграции авто, рестарт `web`/`celery`/`celery-beat`, новых контейнеров нет. Проверено на demo-контуре (auth / search_bankrupts / get_messages / upsert+дедуп / `check_publication` — работают). Подробно — память `efrsb-integration`.

## КоммерсантЪ — публикации в газете (`apps/kommersant`)

**Полное описание:** [`kommersant_integration_claude.md`](./kommersant_integration_claude.md) — жизненный цикл (текст→бланк→отправка→счёт→оплата→выход), денежный контур, генератор текста, бланк заявки на Word content controls, email-транспорт с ящика АУ, приём счёта по IMAP, почтовые креды в профиле, сид, деплой, что не проверено.

Кратко: под-вкладка **«КоммерсантЪ»** вкладки «Публикации» карточки БФЛ (была заглушка в `apps/efrsb`, теперь отдельное приложение). Брат `apps/efrsb`, но с **денежным контуром** (публикация платная предоплатная, ~8–12 тыс ₽, деньги до 14:00 мск даты приёма → номер субботы; статусы `sent/invoiced/paid` раздельные). Три задачи: (1) текст публикации реализации/реструктуризации (`generator.py`, контекст переиспользован из `apps.efrsb`), (2) отправка запроса счёта email с личного ящика АУ (`mailer.py`, регламент `bankruptcy.kommersant.ru/index.php?publemail`), (3) приём счёта по IMAP (матчинг строго по In-Reply-To, beat `kommersant-poll-invoices` 15 мин). 🛑 **Почта АУ — в профиле** (`Employee.kommersant_email`/`kommersant_password_enc` Fernet, карточка только при `role="arbitration"`); вводится **только email+пароль**, SMTP/IMAP по домену (`mail_accounts.py`). 🛑 **Бланк заявки ИД на Word content controls** (`<w:sdt>`) — `render_docx` их не видит, готовый шаблон в `OLD/Коммерсант/`. 🛑 SMTP/IMAP только в Celery (daphne). Разведка по API ИД («Ъ-Публикатор») — память `kommersant-integration-research`. Деплой — обычный `deploy` (зависимостей нет) + вручную `kommersant_seed` + рестарт `celery`/`celery-beat`. 🛑 **Вживую почта не тестировалась** (кредов АУ ещё нет), формулировки шаблонов — DRAFT.

## Отчёты — раздел отчётности (apps/reports)

**Полное описание:** [`reports.md`](./reports.md) — каркас раздела (вкладки), доступ, отчёт «Отдел продаж», модели бюджета (`SalesBudget`/`SalesBudgetEntry`), правило начисления, сортировка/фильтр, деплой, гоччи.

Кратко: раздел `/reports/` (право `can_access_reports` = руководство + админы + бухгалтеры) с вкладками под отчёты. Первый — **«Отдел продаж»**: помесячный реестр входящих платежей-юруслуг (`finance.Payment`; «юруслуги» = флаг **`IncomeType.is_legal_services`**, правится в Справочниках, 🛑 per-DB) + **бюджет отдела продаж** (модели `SalesBudget`/`SalesBudgetEntry`). Строка = операция-поступление («Сумма платежа» целиком vs «Сумма» — юруслуги-часть). Кнопка **«Рассчитать»** начисляет по правилу на строку (`S`=«Сумма»): `S≥5000` и платёж `<8` мес от **`Procedure.intro_date`** первой процедуры → 1000 ₽, `≥8` мес → 400 ₽, `S<5000` → 0, нет даты → как «до 8 мес» (1000). «Начислено» правится онлайн (HTMX), «Итого начислено» — Σ строк. Сортировка по столбцам + фильтр по закреплённому за клиентом сотруднику (`Client.employees`). 🛑 Сиды не нужны (пункт меню — в миграции); деплой — обычный `deploy`; после — отметить юруслуги-типы на КАЖДОМ сервере + заполнять `Procedure.intro_date` для ветки 400 ₽. Память: `reports-module`.

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
- [`accounting_integration_claude.md`](./accounting_integration_claude.md) — Бухгалтерский учёт + ТБанк (выписка р/с поллинг, эквайринг вебхук+prepay, ручное разнесение, подпись/гоччи)
- [`kommersant_integration_claude.md`](./kommersant_integration_claude.md) — публикации в газете «Коммерсантъ» (`apps/kommersant`): текст, бланк заявки (Word content controls), email-подача с ящика АУ, приём счёта по IMAP, денежный контур

Внутренние модули (детально):
- [`reports.md`](./reports.md) — раздел «Отчёты» (`apps/reports`): отчёт «Отдел продаж», бюджет ОП (`SalesBudget`/`SalesBudgetEntry` + правило начисления), сортировка/фильтр, флаг `IncomeType.is_legal_services`
- [`notifications.md`](./notifications.md) — реал-тайм уведомления сотрудникам (`apps/notifications`): флаг `notifies`, получатели «Мой канбан», WS-пуш, панель/кнопки, snooze-beat
- [`рабочее место помощника арбитражного управляющего.md`](<./рабочее место помощника арбитражного управляющего.md>) — раздел процедур БФЛ (`apps/procedure`): модель Дело→Процедуры, движок, карточка+вкладки, таймлайн, сроки-мероприятия, права, деплой, гоччи

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
- `guides/scan-agent.md` — Входящие сканы: установка агента на офисный ПК + работа с лотком `/scans/` (для секретаря/юриста/бухгалтера)
- `guides/admin-overview.md` — приложения, модели, права, сигналы, Celery beat — для понимания общей структуры
- `guides/finance-module.md` — финансовый учёт: модели, генератор графика, статусы, права, события

Прочее:
- `README.md` — общее описание проекта (для GitHub)
