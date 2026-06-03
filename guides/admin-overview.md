# Admin overview — SiriCRM

Эта шпаргалка — для нового разработчика или администратора. Описывает структуру кода в разрезе приложений, ключевые модели, Celery, сигналы и систему прав. Не дублирует `CLAUDE.md` (там — инфраструктура и окружения).

## Приложения

| App | Что внутри | Ключевые модели |
| --- | ---------- | --------------- |
| `apps.core` | Сотрудники, отделы, дашборд-конфиг, справочники, health-endpoint, sidebar | `Employee`, `Department`, `MenuItem`, `Widget`, `DashboardConfig` |
| `apps.crm` | Клиенты, услуги, юр-лица, канбаны, лог событий, API | `Client`, `Service`, `ServiceName`, `Region`, `LegalEntity`, `ClientEvent`, `Message`, `Address`, `PaymentProcedure` |
| `apps.files` | Файловый менеджер клиента (папки/дерево/превью), S3 | `StoredFile`, `Folder` |
| `apps.realtime` | WebSocket consumers (Telegram-чат, уведомления), Channels | — |
| `apps.telegram` | Userbot Telethon, бот, авторизация по TG, identify_helper для модалки «Идентификация» | — |
| `apps.maxchat` | Интеграция с MaxChat | — |
| `apps.consultations` | График консультаций | `Consultation`, `ConsultationResult` |
| `apps.questionnaire` | Анкеты БФЛ с типизированными вопросами, PDF через ReportLab → S3 | `QuestionnaireTemplate`, `Question`, `Response`, `Answer` |
| `apps.devops` | DevOps-панель (handlers, agent, environment) — см. `guides/devops-panel.md` | `Environment`, `DevopsAction`, `DevopsAgentJob` |
| `apps.finance` | Платежи, начисления, справочники типов и касс, генератор графика | `Payment`, `Charge`, `ExpenseType`, `IncomeType`, `IncomingAccount`, `OutgoingAccount` — см. `guides/finance-module.md` |

## Ключевые связи моделей

```
Client ─┬─ services (M:1) ──→ Service ─┬─ employees (M:M через ServiceEmployeeState) ──→ Employee
        │                              ├─ tags (M:M)
        │                              └─ contract_file ──→ StoredFile
        ├─ payments (1:M) ────→ Payment ─→ charge? (FK на Charge)
        ├─ charges (1:M) ─────→ Charge ─→ service? (FK на Service)
        ├─ events (1:M) ──────→ ClientEvent (event_type — большой enum)
        ├─ messages (1:M) ────→ Message (Telegram/MaxChat)
        ├─ addresses (1:M)
        └─ employees (M:M)   ──→ Employee (ответственный)

Employee ─┬─ role: operator|manager|consultant|assitent_legal|lawyer|head_dep|
          │       arbitration|agent|managing_partner|accountant|admin
          ├─ user (OneToOne) ──→ django.contrib.auth.User
          ├─ department ────────→ Department
          └─ services_allowed ──→ ServiceName (M:M, разграничение видимости)
```

`Service.name` — FK на `ServiceName` (справочник: БФЛ, ДТП, …). Все справочники доступны в `/references/` (доступ `is_superuser` или `Employee.role ∈ {admin, head_dep}`).

## Права и helpers

Двухуровневая система: глобальные role-based проверки + object-level через django-rules.

### 1. Role-based — `apps.core.permissions`

Централизованный модуль с константами групп ролей, функциями-предикатами и декораторами. Все новые проверки идут через него, а не inline.

- **Группы ролей** (меняй здесь, не в десятках view):
  - `ELEVATED_ROLES = {admin, head_dep}` — справочники, расширенное меню.
  - `MANAGEMENT_ROLES = {admin, head_dep, managing_partner}` — просмотр чужих канбанов.
- **Предикаты** (все безопасны для суперюзера без `Employee`):
  - `is_superuser(user)`, `is_admin(user)` — superuser или `role=admin`.
  - `is_references_access(user)` — `ELEVATED_ROLES` + superuser. Справочники `/references/`.
  - `is_management(user)` — `MANAGEMENT_ROLES` + superuser.
  - `has_role(user, *roles)` — произвольная проверка.
  - `get_employee(user)` — `user.employee` или `None` без падения.
- **Декораторы view-функций**: `@require_superuser`, `@require_admin`, `@require_references_access`, `@require_management`.
- **DRF permission-классы**: `IsAdmin`, `IsReferencesAccess`, `IsManagement`, `ReadOnlyOrIsAdmin`, `ReadOnlyOrIsManagement`. Все DRF-`ViewSet`'ы CRM/Core используют их.
- Старые имена (`is_superuser/is_admin/is_references_access`) реэкспортируются из `apps.core.views` — поэтому существующие `@user_passes_test(...)` декораторы работают без правок.

Доменные ограничения держим отдельно: `apps.finance.permissions`:
- `can_edit_finance(user)` — `admin`, `accountant`, superuser. Создание/редактирование платежей и начислений.
- `can_delete_finance(user)` — `admin`, superuser. Удаление платежей.
- `can_delete_charge(user, service)` — `admin`/`head_dep`/`consultant`/superuser + `agent` если он есть в `service.employees`.
- Декораторы `@require_edit`, `@require_delete`.

### 2. Object-level — `django-rules` (`apps.crm.rules`)

Для проверок «может ли *этот* пользователь действовать над *этим* объектом» используется [django-rules](https://github.com/dfunckt/django-rules). Установлен пакет `rules`, в `INSTALLED_APPS` — `rules.apps.AutodiscoverRulesConfig` (он сам подхватывает `apps/<app>/rules.py` при старте), в `AUTHENTICATION_BACKENDS` — `rules.permissions.ObjectPermissionBackend` (он включает `user.has_perm('crm.edit_client', client)` и `{% has_perm %}`).

Зарегистрированы пермишены:
- `crm.view_client` / `crm.edit_client` — admin/superuser видит/правит всех; `head_dep` — клиентов своего отдела (предикат `is_in_client_department`); остальные — где они в `Client.employees`.
- `crm.delete_client` — только admin/superuser.
- `crm.view_service` / `crm.edit_service` — admin/head_dep/superuser — все; остальные — если они в `Service.employees` либо тип услуги в `Employee.services_allowed`.
- `crm.delete_service` — admin/head_dep/superuser.

**Важно:** django-rules не фильтрует QuerySet. Для списков (canban, `clients_list`, DRF `list`-action) используем менеджеры из `apps/crm/managers.py`:
- `Client.objects.visible_to(user)` — фильтр клиентов.
- `Service.objects.visible_to(user)` — фильтр услуг.

Логика visible_to **должна совпадать** с предикатами в `apps/crm/rules.py`. Если меняешь бизнес-правило — правь оба места.

### Что использовать в новом коде

| Проверка | Используй |
| -------- | --------- |
| Только роль («админ ли он?») | `is_admin(user)`, `has_role(user, ...)` или `@require_admin` |
| DRF API (read-all / write-admin) | `permission_classes = [ReadOnlyOrIsAdmin]` |
| «Может ли user работать с этим клиентом/услугой?» | `user.has_perm('crm.edit_client', client)` или `@rules_permission_required('crm.edit_service', fn=...)` |
| Фильтрация списка | `Model.objects.visible_to(user)` |
| Кнопка в шаблоне | `{% load rules %}` → `{% has_perm 'crm.edit_client' user client as can_edit %}` |

Не плодите Django Groups/Permissions через БД — текущая система явная, тестируемая и работает без миграций.

## Сигналы и автоматика

| Где | Что делает |
| --- | ---------- |
| `apps.finance.signals` | После `Payment.save` / `Payment.delete` вызывает `Charge.recalc_status()` для связанной charge — выставляет `paid` / `scheduled`. Подключение через `apps.finance.apps.FinanceConfig.ready()` |
| `apps.devops.apps.DevopsConfig.ready()` | Импорт `handlers/` для регистрации хендлеров `@register_handler` |
| `rules.apps.AutodiscoverRulesConfig` | На старте Django ищет `apps/<app>/rules.py` и выполняет `add_perm(...)` — так регистрируются `crm.view_client/edit_client/...` (см. `apps/crm/rules.py`). |
| `apps.crm` (post_save Client / Service) | Часть лога событий, пишется явно из view-функций, а не сигналами — чтобы получить актора (employee) из request |

В целом лог `ClientEvent` создаётся **из view-функций** (а не из сигналов модели) — потому что сигнал не знает, кто инициировал действие. См. `apps/crm/views.py` и `apps/finance/views.py` (helper `_log_event`).

## Celery

### Worker / Beat
- Контейнер `celery` — worker, очередь `celery` по умолчанию.
- Контейнер `celery-beat` — планировщик. После любого изменения `config/celery.py beat_schedule` его надо перезапустить (`deploy` handler делает это автоматически: restart web + celery).
- На prod есть **`flower`** (UI мониторинга) на `flower.siricrm.ru`. На dev — нет.

### Beat schedule (config/celery.py)

```python
beat_schedule = {
    'cleanup-old-logs-daily':      crontab(hour=2, minute=0),   # apps.crm.tasks.cleanup_old_logs(30)
    'generate-daily-report':       crontab(hour=22, minute=0),  # apps.crm.tasks.generate_daily_report
    'sync-employee-status':        60,                          # каждую минуту
    'mark-overdue-charges-daily':  crontab(hour=3, minute=0),   # apps.finance.tasks.mark_overdue_charges
}
```

Все таски — `@shared_task` в `apps.<app>.tasks`. Логика по возможности выносится в `apps.<app>.services` чтобы её можно было вызывать из management-команды или unit-test.

### Очередь `devops`
`devops-runner` — отдельный worker для тяжёлых DevOps-операций (deploy, rebuild, pull_db, push_db). Слушает очередь `devops`. Запуск тасков — через `apply_async(queue='devops')`.

## Шаблоны и UI

- `templates/dashboard.html` — главный layout (НЕ используется `base.html`, dashboard самодостаточен). В нём `<main id="content-area">` куда HTMX подгружает страницы.
- Партиалы — `templates/<app>/partials/`. Открываются через `hx-target="body" hx-swap="beforeend"` (модалки) или `hx-target="#content-area" hx-swap="innerHTML"` (полные страницы).
- Модалки — `<dialog class="modal modal-middle">`. Если модалка открывается **внутри** другой `<dialog>` (через `.showModal()`), новая модалка тоже должна вызвать `.showModal()` иначе окажется под родителем (top-layer спецификация HTML).
- Tailwind pre-compiled (`static/css/tailwind.css`). Сборка только если добавлен класс, которого нет в файле. См. `CLAUDE.md` → «При изменении стилей в шаблонах».

## Структурированные таблицы и сортировка

В крупных таблицах (например, `templates/finance/partials/finance_table.html`) сортировка реализована через клик по `<th>` с обновлением hidden-inputs формы фильтра + `htmx.trigger(form, 'change')`. Это сохраняет состояние сортировки при HTMX-перерисовках.

## События клиента (ClientEvent)

`ClientEvent.EVENT_CHOICES` — открытый enum, расширяется по мере доработки CRM. На момент написания включает (укрупнённо):

- **Общие**: `first_contact`, `status_change`, `client_identified`, `note`
- **Договор**: `contract_created`, `contract_terminated`
- **Сотрудники**: `employee_assigned`, `employee_removed`
- **Производство**: `dept_assigned`, `claim_filed`, `hearing_scheduled`, `procedure_started`, `procedure_ended`
- **Мессенджер**: `dialog_started/ended`, `file_received/sent`
- **Корреспонденция**: `letter_outgoing/incoming`
- **Услуги**: `service_created`, `service_deleted`
- **Консультации**: `consultation_booked/result/transferred/edited`
- **Анкеты**: `questionnaire_created/edited/deleted`
- **Финансы**: `schedule_created`, `schedule_updated`, `payment_in_*`, `payment_out_*`, `charge_overdue`
- **Система**: `system`

Добавляя новый тип — добавьте в enum + миграцию `AlterField` + создавайте запись из view (helper `_log_event` в `apps/finance/views.py` как пример).

## Sessions

Сессии хранятся в **Redis** (`SESSION_ENGINE = "django.contrib.sessions.backends.cache"`). Это значит:
- При `pull_db`/`push_db` (drop schema) пользователей не выкидывает на логин.
- При `flushdb` Redis (или его рестарте без persistence) все будут выкинуты.

## Тонкости среды разработки

- **Не использовать `cd <dir>` перед `git ...`** — это вызывает permission prompt в Claude Code. Все `git ...` работают из cwd.
- **`docker compose restart <svc>`** НЕ перечитывает `env_file`. Для смены env — `up -d --force-recreate <svc>`.
- **После рестарта `web` на dev** часто нужен `restart nginx` — внутренний IP контейнера меняется.
- **Шаблоны в prod-конфиге кэшируются** (`cached.Loader`). После изменения шаблона — `restart web`.
- **HTMX-партиалы кэшируются в браузере** — пользователю давать `Ctrl+Shift+R`.

## Куда заглянуть дальше

- `CLAUDE.md` — окружения, инфраструктура, гайдлайны кода
- `guides/devops-panel.md` — как пользоваться DevOps-панелью (для суперюзера)
- `guides/finance-module.md` — детально про финансовый модуль
- `docs/PRODUCTION.md` / `docs/DEV_MIGRATION.md` — развёртывание серверов
- `docs/legacy-quickstart.md` — устаревший quickstart (для исторического контекста)
