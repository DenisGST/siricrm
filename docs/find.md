# Поиск (глобальный поиск + фильтр канбана)

Вся механика поиска по клиентам/данным и фильтрации канбана. Кратко в `CLAUDE.md` (раздел «Поиск»).

## Глобальный поиск (шапка дашборда)

- **Вью:** `apps/crm/views.py:global_search` · **URL:** `/api/global-search/` (`name="global_search"`) · **инпут:** `#global-search` (`name="q"`) в `dashboard.html` · **результаты:** партиал `templates/crm/partials/global_search_results.html` (HTMX swap по мере ввода).
- **Что ищет** (группы в выдаче): Клиенты (ФИО / `Client.phone` / `ClientPhone.phone`), Чат (клиенты для чат-модалки), Юр. лица, Файлы (`ClientFile.name`, мультислово — AND через `.split()` + цепочка `.filter`), Сообщения.
- **Права/доступ:** каждой записи вью проставляет `c.no_access = c.id not in Client.objects.visible_to(user)...`. Шаблон затеняет такие строки (`.gs-no-access`) и подменяет клик на `globalSearchNoAccess()` → `showToast(...)`.

### Клик по найденному клиенту — ДВА сценария
1. **Клик по строке** → `pickClientForKanbanFilter(fio)` (`dashboard.html`): кладёт ФИО в `#flt-q`, **снимает** `#flt-cid`, шлёт `kanbanRefresh`. Канбан показывает **всех похожих по ФИО** (тёзок). Это «по схожести».
2. **Кнопка 🎯 «Только этот»** → `pickExactClientForKanban(cid)`: ставит скрытый `#flt-cid`=id, **очищает** `#flt-q`, шлёт `kanbanRefresh`. Канбан показывает **ровно одну карточку** (точно по id). Решает проблему клиента без фамилии (напр. только «Владимир» → раньше фильтр по ФИО показывал всех Владимиров).

`cid` и `q` **взаимоисключающие** (один путь чистит другой; ручной ввод в `#flt-q` тоже сбрасывает `#flt-cid`).

### Кнопки действий в строке результата (`.gs-act`)
🎯 «Только этот» · 💬 чат (`openTelegramChatModalForClient`) · 💰 финансы (`finance:finance_modal`) · 📁 файлы (`files:manager`) · 🕘 события (`client_events_modal`) · 📞 звонок (`tel:`). Все по `c.id`.
- **Стиль:** серые по умолчанию, цветные на hover — свой цвет через CSS-переменную `--gs-hc` на каждой кнопке (`static/css/style.css`). 🎯 — эмодзи, серый через `filter:grayscale(1)`, на hover `filter:none`.
- 🛑 **Гочча цвета иконок:** правило `.global-search-item svg { color: … }` задаёт цвет **прямо на `<svg>`** (stroke=currentColor) → наследуемый от кнопки цвет на hover НЕ доходит до иконки. Лечится `.global-search-item .gs-act:hover svg { color: var(--gs-hc) }`.
- 🛑 **Иконки `{% icon %}`:** в наборе (lucide) **нет** `target` (рендерит `<!-- icon 'target' not found -->` → пустая кнопка). Поэтому «Только этот» — эмодзи 🎯, а не `{% icon %}`. Перед использованием новой иконки проверять, что она есть.

## Фильтр главного канбана

- **Колонки:** `apps/crm/views.py:kanban_column(status)`; шаблон `templates/crm/kanban.html` (`hx-trigger="load, kanbanRefresh from:body"`, `hx-include="#kanban-filter-form"`).
- **Параметры фильтра** (GET): `q` (мультислово: AND по словам, OR по полям `first/last/patronymic/phone/phones__phone`), **`cid`** (точно по id, имеет приоритет над `q`), `employee`/`service_employee`/`ms_status`/`created_from`/`created_to`.
- **Форма фильтра** `#kanban-filter-form` (поповер, `dashboard.html`): `#flt-q` (`name=q`), скрытое `#flt-cid` (`name=cid`), `#flt-employee` и т.д. `onchange` формы → `kanbanRefresh`.
- **JS** (`dashboard.html`):
  - `pickClientForKanbanFilter(fio)` — q-путь (чистит cid).
  - `pickExactClientForKanban(cid)` — cid-путь (чистит q).
  - `#flt-q` `oninput` — debounce 400мс + чистит cid.
  - `resetKanbanFilters()` — `form.reset()` **+ явно** `flt-cid.value=''` и `flt-q.value=''` (🛑 `form.reset()` не всегда сбрасывает программно выставленные скрытые поля → «Сбросить фильтр» не снимал cid), затем `kanbanRefresh` + индикатор.
  - `updateKanbanFiltersIndicator()` — `new FormData(form)`: любое непустое значение → фильтр активен (точка + кнопка «Сбросить» видимы). `cid` в форме → считается активным.
- Две кнопки «Сбросить» (индикатор в шапке + внутри поповера) → обе зовут `resetKanbanFilters()`.

## Поиск в чат-модалке (отдельный механизм)

Левый список чат-модалки — свой поиск/скоуп: `telegram_clients_list` (`name="q"` в `#telegram-search`), подсветка через `window._activeTelegramClientId`, **`?pin_client_id=<uuid>`** гарантирует попадание клиента в page=1 (для `openTelegramChatModalForClient`). Подробно — `docs/ui-conventions.md` (чат-модалка).

## Поиск в канбане услуг / моём канбане
`services_kanban_column` / `my_kanban_column` — свои `q`-фильтры по тому же принципу (без `cid`).
