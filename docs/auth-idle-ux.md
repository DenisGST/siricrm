# Auth: multi-tab logout + idle UX

Подробности по авторизационному UX. Кратко в `CLAUDE.md` (раздел «UI/UX: auth + idle UX»).

## Multi-tab logout (grace-версия — НЕ шлёт beacon на unload)
`static/js/multi-tab-logout.js` (подключён в `dashboard.html`, `arbitr/_layout.html`, `devops/_layout.html`). Задача — завершать сессию при закрытии приложения, но НЕ выкидывать на reload/навигации/закрытии одной из нескольких вкладок.

> ⚠ **История (важно не вернуть как было):** раньше скрипт слал `sendBeacon('/accounts/logout/')` прямо в `beforeunload` (если вкладка последняя и нет метки `sirius_internal_nav`). Но `beforeunload` НЕ отличает закрытие от reload/навигации, и метка не ставилась на JS-reload, `HX-Redirect`, back/forward, www↔без-www → **десятки ложных авто-логаутов в день** (на проде доходило до 75/день): юзера выкидывало → цикл logout→login→тяжёлая перезагрузка дашборда («постоянно грузится»). Поэтому **beacon на unload убран совсем**.

Текущая схема (чисто клиентская):
1. **heartbeat**: каждая вкладка раз в 3с пишет «я жива» в `localStorage.sirius_tabs`.
2. **beforeunload**: вкладка удаляет свой heartbeat и ставит метку `sirius_pending_logout=ts`. **Логаут не шлёт.**
3. **Загрузка любой страницы приложения снимает метку** (значит был reload/навигация) → ложного логаута нет. ← поэтому скрипт обязан быть на каждом layout.
4. Живая вкладка раз в 2с: если метка висит дольше grace (5с) и есть живые вкладки — снимает метку (закрыли одну из многих).
5. **Реальное закрытие последней вкладки**: метка остаётся, JS уже не работает, логаут активно не шлётся — сессию подчищает **idle-таймаут (10 мин)**. Это сознательный компромисс ради нуля ложных логаутов (фундаментально: после закрытия вкладки JS не выполнить, а синхронный beacon в `beforeunload` и есть источник ложных срабатываний).

Кнопка «Выйти» (явный POST `/accounts/logout/`) работает как раньше — это отдельный путь.

## Idle UX — warning + locked-overlay (без редиректа на /login/)

`IDLE_TIMEOUT_MINUTES = 10` (`config/settings/base.py`). Поток в `dashboard.html` (IIFE снизу) + `apps/core/views.py` + `apps/core/middleware.py`:

1. JS poller каждые **15с** → `GET /api/session/idle-check/` (этот путь в `IDLE_IGNORE_PREFIXES` middleware'а → НЕ обновляет `last_activity` и НЕ дёргает auto-logout сам).
2. Ответ: `{authenticated, idle_seconds, timeout_seconds, warning_seconds=60, logout_reason}`.
3. За **60с** до таймаута → **warning-модалка** (`#idle-warning`, z-index 9998) с countdown'ом и кнопкой «Остаться» (POST `/api/session/stay/` → обновляет `last_activity`).
4. После **600с** middleware (`IdleAutoLogoutMiddleware`) делает `auth_logout()` и кладёт `logout_reason` в сессию.
5. Следующий poll получает `authenticated=false` → **locked-overlay** (`#idle-locked`, z-index 9999) с inline-формой логина (POST `/api/session/login/` → `authenticate + login` в той же сессии, потом `window.location.reload()`).

**Keepalive: активность продлевает сессию через poll (НЕ отдельный интервал).** `last_activity` на сервере обновляет только non-ignored HTTP-запрос. Но клики/скролл/ввод/движение мыши, а особенно чат по WebSocket (`/ws/` в IGNORE) и чтение длинных страниц, HTTP не шлют → активного юзера выкидывало по таймауту. Решение: слушаем `mousedown/mousemove/keydown/touchstart/input/scroll/wheel` (capture-фаза → ловится и в `<dialog>`/модалках) и пишем `_lastActivity`; `poll()` шлёт `GET /api/session/idle-check/?a=1`, если активность была в окне опроса, а вьюха при `a=1` обновляет `last_activity` и возвращает `idle_seconds=0`. Привязка к НАДЁЖНОМУ поллеру обязательна: прежний отдельный `setInterval`-keepalive (POST `/stay/`) на практике почти не срабатывал (троттлинг фоновых вкладок, гонка с warning-модалкой) — на проде было ~0 вызовов `/stay/` против ~42 idle-check/сек. **Гочча:** юзеры с уже открытыми вкладками крутят старый JS до перезагрузки страницы — фикс активируется по мере reload/релогина.

**Guard от runaway:** IIFE начинается с `if (window.__siriIdleInit) return; window.__siriIdleInit = true;` — при `hx-boost`/повторной вставке скрипта IIFE не регистрировал второй `setInterval(poll)` (был runaway idle-check).

> 🛑 **НЕ ЛОМАТЬ — инварианты keepalive (иначе активных юзеров снова начнёт выкидывать).** Менять только осознанно, все пять держать вместе:
> 1. **`/api/session/idle-check/` ОБЯЗАН оставаться в `IDLE_IGNORE_PREFIXES`** (middleware). Если убрать — каждый poll (раз в 15с) станет «активностью» и сессия будет жить вечно у всех → авто-логаут не сработает никогда.
> 2. **Ветка `if request.GET.get("a") == "1"` в `session_idle_check` — это и есть keepalive.** Она и только она обновляет `last_activity` при активности (idle-check в IGNORE, middleware его не трогает). Удалишь/сломаешь условие → активность перестанет продлевать сессию.
> 3. **`poll()` в `dashboard.html` должен слать `?a=1`, когда `_lastActivity` свежий** (в окне опроса). Не выкидывать параметр, не выносить keepalive в отдельный `setInterval` — отдельный интервал на практике НЕ работает (троттлинг фоновых вкладок, гонка с warning-модалкой), проверено на проде (~0 вызовов `/stay/`).
> 4. **Слушатели активности на `document` — `capture:true, passive:true`** для `mousedown/mousemove/keydown/touchstart/input/scroll/wheel`. Capture обязателен, иначе ввод внутри `<dialog>`/модалок и чата не ловится. `passive:true` — чтобы не мешать скроллу/DnD.
> 5. **Guard `window.__siriIdleInit`** не убирать — иначе runaway idle-check.
>
> И помни: **правка JS в `dashboard.html` доходит до юзера только после reload страницы** — открытые вкладки крутят старый JS. После изменений просить `Ctrl+Shift+R`.

**Ключевые механизмы (всё в dashboard.html IIFE):**
- `visibilitychange` + `focus` → `poll()` сразу. Без этого browser throttle'ит `setInterval` в фоновых вкладках до 1/мин → юзер мог вернуться и кликнуть до того как poll увидит logout.
- Конец warning-countdown → `poll()` сразу (а не ждать 15с).
- **`htmx:beforeOnLoad`** — перехват ответов с `HX-Redirect: /accounts/login/` или статусом `401`. Вместо HTMX-редиректа на login (через `HtmxLoginRedirectMiddleware`) показываем locked-overlay. Этим закрываем гонку «юзер кликнул до того как poll увидел logout» — первый же XHR-ответ показывает оверлей.
- **Capture-phase trap** для `click` / `keydown` / `submit`: пока `_showLocked === true`, всё что не внутри `#idle-locked` блокируется. Защита от `<a href>` навигации (которая идёт мимо HTMX) и от элементов с z-index ≥ 9999.

**Эндпоинты `/api/session/`:**
- `idle-check/` — публичный (auth не обязателен), в `IDLE_IGNORE_PREFIXES`. При `?a=1` (клиент сообщил о реальной активности) обновляет `last_activity` и отдаёт `idle_seconds=0` — основной keepalive. Пустой поллинг (без `a=1`) активностью не считается.
- `stay/` — `@require_POST`, требует auth, обновляет `last_activity`. НЕ в IGNORE_PREFIXES. Сейчас используется только кнопкой «Остаться» в warning-модалке.
- `login/` — `@require_POST`, JSON `{username, password}` → `authenticate + login`. НЕ в IGNORE_PREFIXES.
