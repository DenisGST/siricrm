# Лог клиента: события + действия (`ClientLogEntry`) — СОБЫТИЙКА

Полное описание концепции «СОБЫТИЙКА». Кратко в `CLAUDE.md`.

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
