# Уведомления — реал-тайм оповещения сотрудников (`apps/notifications`)

Полное описание фичи. Кратко — в `CLAUDE.md` (секция «Уведомления»).

**Суть:** когда по клиенту/услуге происходит событие или действие, требующее
внимания (поступил платёж, парсер нашёл дело, передали клиента в отдел, сменился
статус и т.п.), сотрудникам, **работающим с этим клиентом** («Мой канбан»),
прилетает уведомление — на сайте (колокол в шапке + панель) и (в планах) в
Telegram-бот. Сотрудник реагирует кнопками; реакция пишется в событийку.

Деплой на прод: 14.06.2026, ветка `feat/production-ready`, коммит `a38b82f`.
Фича **дормантна по умолчанию** — пока на типах не включён флаг `notifies`,
уведомления не генерируются (флаги per-DB).

---

## Модель `Notification` (`apps/notifications/models.py`)

Фан-аут: одно событие → **по строке на каждого получателя** (каждый реагирует
независимо). Поля:

- `recipient` (FK `core.Employee`, CASCADE), `client` (FK `crm.Client`, CASCADE).
- `source` (FK `crm.ClientLogEntry`, **SET_NULL**) — запись событийки, породившая
  уведомление. `text` (CharField 500) — снимок текста (переживает изменение source).
- `hint` (CharField 255) — подсказка-что-делать (из `notify_hint` типа).
- `status` ∈ `new` → `accepted`(в работе) → `done`(исполнено) / `rejected`(отклонено)
  / `snoozed`(отложено) / `acknowledged`(ознакомлен). `CLOSED_STATUSES =
  (done, rejected, acknowledged)`; property `is_active` = не закрытое.
- `snooze_until` (DateTime) — до когда отложено.
- `responded_at`, `responded_via` (`web`/`telegram`), `response_log`
  (FK ClientLogEntry — запись-ответ в событийке).
- `tg_message_id` — задел под Telegram-зеркало (Stage C).
- Индексы: `(recipient, status)` — бейдж/список; `(status, snooze_until)` — beat.

Бейдж = `filter(recipient=emp, status="new").count()`.

---

## Триггер: флаг `notifies` в справочниках

На `crm.EventType` и `crm.ActionType` добавлены поля (миграция `crm.0083`):
- `notifies` (bool) — «порождает уведомление»;
- `notify_hint` (CharField) — текст-подсказка в строке уведомления.

Редактируются в `/references/event-types/` и `/action-types/` (формы
`EventTypeForm`/`ActionTypeForm`, модалки `event_type_form_modal.html` /
`action_type_form_modal.html`). 🛑 После правок — `client_log.invalidate_cache()`
(уже дёргается из CRUD справочников).

**Хук генерации** — в `apps/crm/client_log.py:_maybe_notify(entry)`, вызывается
из `record_event`/`record_action` после создания записи: если `type.notifies` →
`services.notify(entry)`. Обёрнут в try/except — **уведомления не должны ронять
запись лога**.

🛑 **Анти-дубль `spawns_event`:** действие со `spawns_event` создаёт две записи
(action + порождённое event). Чтобы не задвоить — `record_action` уведомляет
**на стороне порождённого события** (а «одиночное» действие без spawns_event — на
самом действии). Для пар флаг `notifies` ставить на событии (так написано в
help_text формы действия).

---

## Получатели: `services.recipients_for_client(client)`

Союз «Мой канбан» = ответственные (`Client.employees`) ∪ исполнители услуг
(`Service.employees` → `Employee.assigned_services`) ∪ сотрудники отдела текущего
этапа услуги (`Service.common_status.department`). Совпадает с логикой
`Client.objects.visible_to` для рядового сотрудника.

🛑 **Считать ТРЕМЯ отдельными индексированными запросами и объединять pk в
Python.** Один общий `Employee.objects.filter(Q|Q|Q).distinct()` на боевой БД
порождает join Employee × услуги × отделы с OR — **план взрывается (>90 секунд!)**.
Переписано на три быстрых запроса → 0.028с. Это критично: `notify()` вызывается
inline на каждом событии.

**Адресные уведомления:** `notify(entry, recipients=[...])` — если у события есть
явный получатель (передача услуги конкретному сотруднику/отделу), передавать его
явно; иначе — союз. `exclude_actor=True` — автора действия не уведомляем.

---

## Реакция: `services.respond(notification, action, *, employee, via, snooze_until, comment)`

`action` ∈ `accept` / `done` / `reject` / `snooze` / `acknowledge`. Меняет статус
и **пишет действие в событийку** через `record_action` кодами (сиды `crm.0084`/`0085`):

| action | статус | код ActionType |
|--------|--------|----------------|
| accept | accepted | `notif_accepted` |
| done | done | `notif_done` |
| reject | rejected | `notif_rejected` |
| snooze | snoozed | `notif_snoozed` |
| acknowledge | acknowledged | `notif_acknowledged` |

`comment` (причина отклонения) дописывается к тексту записи (`текст — причина`).
🛑 **Отложка тоже фиксируется в событийке** (требование ТЗ).

---

## WebSocket-пуш (`apps/realtime/utils.py`)

Поверх существующей группы `user_notifications_{user.id}` и хэндлера `notify`
консьюмера (он шлёт `event["html"]` как OOB-swap):
- `push_notification(notification)` — OOB-бейдж + маркер `data-notification-new`
  (звук/рефреш открытой панели у получателя).
- `push_notification_badge(employee)` — просто пересчитать бейдж (после реакции).
- `_notif_badge_html(employee)` — рендер `notifications/partials/badge_oob.html`
  (серый/0 → красный/N).

Слушатель в `dashboard.html` (`htmx:wsAfterMessage`): на `data-notification-new` —
звук + если панель открыта, рефреш `#notif-inner` текущей вкладки.

---

## Web-UI

**Колокол** (`dashboard.html`) — `toggleNotifications()` открывает/закрывает панель;
поллер бейджа → `notifications:badge` (фолбэк к WS, раз в 20с). Кнопка помечена
`data-notif-bell`.

**Панель** `/notifications/panel/` — фиксирована справа-сверху, ширина 860px.
Статичный шелл (`#notifications-panel` + шапка) + подменяемое тело `#notif-inner`
(вкладки + список). 🛑 При смене вкладки/реакции перерисовывается **только тело**
(`hx-target="#notif-inner" hx-swap="innerHTML"`) — иначе мигал бы весь
`position:fixed`-шелл. Вкладки: **Новые / В работе / Отложенные / Закрытые** (со
счётчиками). Строка: `время · ФИО клиента (клик → чат) · текст`; подсказка —
hover (`title`).

**Кнопки** — SVG-иконки Lucide, серые по умолчанию, **цветные при наведении**
(per-кнопка CSS-переменная `--nt-hc`, как `.gs-act` в поиске). Порядок и смысл:

| Иконка | Действие | Цвет hover |
|--------|----------|-----------|
| ✓ `check` | Ознакомлен (без действий) | зелёный `#22c55e` |
| 👍 `thumbs-up` | Принять в работу (скрыта, если уже принято) | янтарь `#f59e0b` |
| ✓✓ `check-check` | Исполнил | синий `#3b82f6` |
| ✕ `x` | Отклонить — **дропдаун с полем причины** | красный `#ef4444` |
| 🔔 `bell` | Напомнить позже — пресеты + datetime | фиолет `#8b5cf6` |

«Напомнить позже»: пресеты (15м / 1ч / 3ч / завтра 10:00) + `<input
datetime-local>` (приоритет, трактуется в МСК). Парсинг — `views._parse_snooze`.

**Закрытие панели:** ✕ / **Esc** / **клик вне** — IIFE-скрипт в шелле, общая
`window.__closeNotifPanel` (снимает слушатели). Клик по колоколу
(`data-notif-bell`) игнорируется обработчиком «вне» — закрытие отдаёт toggle.

🛑 **Дропдауны snooze/reject — `position:fixed` через JS** (`onFocusIn` в шелле):
меню выше короткого списка и обрезалось бы `overflow` контейнера (и снизу, и на
коротком списке из 3 строк — в любую сторону). Выносим из скролл-контейнера и
позиционируем у триггера (вниз; вверх — только если снизу не хватает места до
края экрана). DOM-иерархия не меняется → клик-внутри/Esc работают.

🛑 **`{% load icons %}`** обязателен в `panel_inner.html` (партиал рендерится
отдельно от dashboard). Без него — `Invalid block tag 'icon'`.
🛑 Инлайн-скрипты модалки — в **IIFE** (htmx переисполняет при каждом открытии;
top-level const/let дали бы «already declared»).

**Views** (`apps/notifications/views.py`): `badge`, `panel` (полный шелл),
`panel_list` (только тело), `respond` (реакция → тело + OOB-бейдж).
Получатель — `request.user.employee`; реагировать можно только на свои.

---

## Stage B — авто-возврат отложенных (`tasks.revive_snoozed`)

Celery-beat-таск `notifications.revive_snoozed` (`config/celery.py`, **каждые 60с**):
находит `snoozed` с `snooze_until <= now` → переводит в `new`, чистит
`snooze_until`, пушит получателю (бейдж + маркер «новое»). 🛑 На проде при деплое
нужен рестарт `celery` **и** `celery-beat` (deploy-handler их перезапускает).

---

## Миграции

- `crm.0083` — `notifies` + `notify_hint` на EventType/ActionType.
- `crm.0084` — сид ActionType ответов (`notif_accepted/done/rejected/snoozed`).
- `crm.0085` — сид `notif_acknowledged`.
- `core.0023` — `Employee.telegram_chat_id` (nullable, unique; задел под TG-бот).
- `notifications.0001` — модель Notification; `0002` — статус `acknowledged`.

---

## Деплой / эксплуатация

- **`deploy`, не `rebuild`** — новых зависимостей нет.
- 🛑 На проде после деплоя рестартить `celery` + `celery-beat` (новый beat-таск).
  Deploy-handler это делает; при ручном `rebuild`/частичном — не забыть.
- 🛑 Флаги `notifies` — **per-DB** (как Meta-шаблоны). Включать на dev И на prod
  отдельно, в `/references/`.
- `static/icons/line/check-check.svg` — добавленная иконка (двойная галка,
  официальный Lucide-путь). `{% icon %}` читает исходник, не manifest → проблем
  со strict-static нет.

---

## Карта файлов

```
apps/notifications/
  models.py      — Notification
  services.py    — notify(), respond(), recipients_for_client()
  views.py       — badge / panel / panel_list / respond
  urls.py        — namespace "notifications"
  tasks.py       — revive_snoozed (beat)
  migrations/    — 0001, 0002
apps/crm/client_log.py            — хук _maybe_notify
apps/crm/models.py                — notifies/notify_hint на типах
apps/crm/migrations/0083..0085
apps/core/models.py               — Employee.telegram_chat_id
apps/core/migrations/0023
apps/core/forms.py                — поля в формах типов
apps/realtime/utils.py            — push_notification / _badge / _notif_badge_html
config/celery.py                  — beat-запись revive-snoozed
templates/notifications/panel.html, partials/panel_inner.html, partials/badge_oob.html
templates/core/partials/{event,action}_type_form_modal.html  — тогглы notifies
templates/dashboard.html          — колокол + WS-слушатель
static/icons/line/check-check.svg
```

---

## Stage C — Telegram-бот (НЕ сделано, следующая итерация)

Отдельный бот (`NOTIFY_BOT_TOKEN`) + привязка `Employee.telegram_chat_id` кодом
(Redis `notify_bind:<code>`, сотрудник жмёт Start → вводит код из профиля).
Зеркало каждого уведомления — сообщение с inline-кнопками; callback-handler =
та же `services.respond` (`via="telegram"`), `tg_message_id` для редактирования
карточки при реакции с сайта (и наоборот). Поллинг getUpdates через beat
(webhook не работает — split-tunnel, см. `telegram_integration_claude.md`).
🛑 Отдельный токен (не общий `TELEGRAM_BOT_TOKEN`) — иначе конфликт getUpdates
с leads/monitor-ботами.

Опц.: вынести `notify()` в Celery при нагрузке (сейчас inline в запросе).
