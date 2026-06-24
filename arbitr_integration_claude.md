# Арбитраж — мониторинг kad.arbitr.ru (`apps/arbitr/`)

**Цель:** автоматически следить за делами клиентов БФЛ на kad.arbitr.ru. Сотрудник в карточке услуги ставит «иск отправлен в суд» → создаётся `ArbitrCase(status='searching')` → парсер ищет дело по ФИО → сотрудник подтверждает найденную карточку → `status='monitoring'` → парсер регулярно собирает события (ходатайства, определения, расписания заседаний) и документы.

## Контейнер `arbitr-runner` (отдельный)

- `docker/arbitr/Dockerfile` — python:3.11-slim + **google-chrome-stable** (из официального deb-репо) + **chromedriver** (под мажор-версию Chrome, тянем с chrome-for-testing) + **Xvfb** (виртуальный X-сервер). selenium 4.27 + selenium-stealth.
- `docker/arbitr/entrypoint.sh` — поднимает Xvfb на `:99` в фоне → `exec celery worker -Q arbitr`. `ENV DISPLAY=:99` в Dockerfile — чтобы и worker, и `docker exec` (kad_probe) ходили на один Xvfb.
- Сервис в обоих compose-файлах: `shm_size: '2gb'`, `concurrency=1`, `max-tasks-per-child=20` (Chromium течёт).
- Очередь celery `arbitr` — `CELERY_TASK_ROUTES['arbitr.*']={'queue':'arbitr'}` в `base.py`. Общий celery worker эти таски не подхватит.

## Почему Selenium, а не Playwright

Сначала пробовал Playwright headless (jammy/noble образы microsoft/playwright-python). **Не пошло**: kad возвращал captcha challenge на любой data-запрос (`/Card/<uuid>`, `/Kad/SearchInstances`) — даже с `playwright-stealth`. Главная страница отдавалась (она статика), но контент — никогда. Selenium + selenium-stealth с **headed Chromium через Xvfb** проходит anti-bot kad.

## Антидетект-стек (`apps/arbitr/parsers/kad.py:KadSession`)

- **headed Chrome через Xvfb** (НЕ headless) — главное оружие.
- `selenium-stealth(vendor='Google Inc.', platform='Win32', webgl_vendor='Intel Inc.', renderer='Intel Iris OpenGL Engine')` — глубже подделывает navigator + WebGL, чем playwright-stealth.
- Chrome args: `--disable-blink-features=AutomationControlled`, `excludeSwitches=['enable-automation']`, `useAutomationExtension=False`, `--disable-webgl --disable-gpu --enable-unsafe-swiftshader --disable-accelerated-2d-canvas` (kad фингерпринтит WebGL renderer).
- UA `Mozilla/5.0 (Windows NT 10.0; Win64; x64) … Chrome/120` — Linux-UA подозрительный для kad.

## Главный трюк: «прогрев» сессии

Прямой `GET /Card/<uuid>` всегда возвращает captcha. Но если в той же Chrome-сессии сначала сделать **поиск через UI** (главная → ввод в `[placeholder="например, А50-5568/08"]` → клик `[alt="Найти"]` → ждать `.b-case-loading` 10-15с) — kad ставит session-cookies и дальше отдаёт `/Card/` без капчи. Это и есть `KadSession._warm_up()` — один раз на жизнь сессии, потом много операций.

## Capture detection

Маркеры в `apps/arbitr/parsers/kad.py:CAPTCHA_MARKERS`:
- `id="tokenFrom"` — форма submit'а captcha challenge
- `pravocaptcha.execute` — JS-вызов на странице challenge
- `"Доступ заблокирован"` — IP-блок (HTTP 451)
- `"Подтвердите, что вы не робот"`

**НЕ маркер** — `b-pravocaptcha` сам по себе или текст «Превышено количество попыток»: оба присутствуют в обычной kad-странице как `<script type="x-jquery-tmpl">`-шаблон → ложные срабатывания.

## Две Chrome-сессии: main (search/parse) + download (PDF)

🛑 **`plugins.always_open_pdf_externally: True` ломает поиск на kad** — kad-anti-bot проверяет `navigator.plugins`, не находит PDF Viewer → click «Найти» молча не делает XHR. Эта связка search↔PDF prefs не очевидна. Найдено эмпирически 11.06.2026 пошаговым isolating'ом prefs.

Архитектура:
- `KadSession(download_mode=False)` — **baseline Chrome без PDF prefs**. Используется для search + parse_case. Поведение — как в эталонном коде пользователя: простой `send_keys → click [alt="Найти"]`.
- `KadSession(download_mode=True)` — Chrome с `plugins.always_open_pdf_externally=True` + `download.default_directory` + CDP `Page.setDownloadBehavior`. Используется ТОЛЬКО для PDF download'а — search/parse в ней сломаны (детектится anti-bot'ом).
- **Cookies-bridge** между сессиями: после parse_case `kad.driver.get_cookies()` → `KadSession(download_mode=True).load_kad_cookies(cookies)` → download-сессия «доверена» kad без повторного UI-поиска (search в download_mode не работает).

Поток в `_parse_one`:
```python
with KadSession() as main:              # baseline
    info = main.parse_case(case.kad_url)
    _persist_case_info(case, info)      # ArbitrEvent + ArbitrAttachment в БД
    cookies = main.driver.get_cookies()
# main session закрыта → открывается download session
_download_new_attachments(case, source_cookies=cookies)
    # внутри: KadSession(download_mode=True)
    # → load_kad_cookies(cookies)
    # → driver.get(case.kad_url)  # активация trust через карточку
    # → for att: download_pdf(att.kad_url, referer=case.kad_url)
    # → upload_file_to_s3 + StoredFile.create
```

Замер на боевом А12-33291/2024: **171 events + 43/44 PDF в S3 за 2:19** (1 PDF kad отдал пустым — edge case, не ошибка).

## `.b-case-loading` скрывается через computed-style

kad скрывает loader CSS-классом/родительским display, не inline `style="display: none"`. Старый CSS-селектор `.b-case-loading:not([style*="display: none"])` всегда true → `_wait_search_loaded` зависал на 25с. Чек теперь через JS `getComputedStyle(el).display !== 'none'`.

## StoredFile.filename truncate

У kad заголовки документов бывают по 300+ символов («[Подписано] Отложить судебное разбирательство (ст.157, 158, 225_15 АПК)»). `StoredFile.filename` = CharField(max_length=255). Перед `StoredFile.create` обрезаем до 250 (`safe_name = head[: 250 - len(ext) - 4] + "…." + ext`).

## Селекторы (по состоянию 2026)

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

## Идемпотентность событий

У kad `data-id` на событии **всегда пуст** — поэтому `kad_event_id` генерируем как `sha1(instance_id|event_date|kind|title|description)[:24]`. UNIQUE-индекс `(case, kad_event_id)` в `ArbitrEvent.Meta.constraints` защищает от дублей при повторном парсинге.

## Восстановление судьи

kad на уровне шапки инстанции имя судьи не пишет — но в каждом событии `kind == "Событие"` ФИО судьи лежит в `case-subject`. `_fill_instance_judges` берёт самое частое ФИО (regex `^[А-Я][а-я]+(\s+[А-Я]\.\s*[А-Я]\.?)?$`) в рамках инстанции.

## Smart-parser архитектура (2026-06-24, актуальное)

Старые `kad_monitor_pending`/`kad_monitor_case` (батч-таски) **отключены** (`PeriodicTask.enabled=False`) — выгоняли всю пачку в один Chrome за раз, ловили капчу после ~20 PDF-download'ов, флудили алёртами. Сейчас работает **3 параллельных runner'а с динамическим throttle и ротацией outbound IP**.

### Контейнеры
- `arbitr-runner` (id=`a`, env `ARBITR_RUNNER_ID=a`, очередь `arbitr_a,arbitr`)
- `arbitr-runner-b` (id=`b`, очередь `arbitr_b`) — `image: siricrm-arbitr-runner:latest` (переиспользует образ a, экономия диска)
- `arbitr-runner-c` (id=`c`, очередь `arbitr_c`) — то же

Routes в `config/settings/base.py`: `arbitr.kad_smart_one_<a/b/c>` → `arbitr_<a/b/c>`. Beat-tasks `arbitr-kad-smart-one-<a/b/c>` interval 10с каждый.

### Алгоритм каждого тика (`apps/arbitr/tasks.py:_kad_smart_one`)
1. Прочитать `arbitr:runner_ip:<id>` из Redis (пишет rotator). Пусто → `skipped: runner_disabled`.
2. `cooldown.is_active(runner_ip)` — этот IP в 12ч капче? → `skipped: captcha_cooldown`.
3. Per-runner throttle `arbitr:smart_throttle_until:<id>` (ISO end-stamp) → `skipped: throttle`.
4. Per-runner lock `arbitr:smart_lock:<id>` (TTL 10мин) → `skipped: lock_busy`.
5. **Атомарный «забор» кейса**: `SELECT FOR UPDATE SKIP LOCKED` → SEARCHING приоритет (`next_search_at <= now OR NULL`), иначе MONITORING (`next_parse_at <= now OR NULL`). Сразу ставим `next_*_at = now + 30мин` («резерв»), чтоб другие runner'ы не взяли тот же кейс.
6. Парсим (`_search_one`/`_parse_one`, оба принимают `runner_ip`). После парсинга `_parse_one` ставит финальный `next_parse_at = now + 24ч`.
7. Per-runner throttle по результату:
   - **ok + что-то новое** (events>0 OR files>0): `random.randint(180, 900)` = 3-15 мин
   - **ok + ничего нового**: 10с
   - **error/nothing**: 60с
   - **captcha**: ничего не ставим — `handle_captcha` уже включил 12ч cooldown ДЛЯ ЭТОГО IP
8. Каждые `BREAK_EVERY=8` успехов подряд (на этом runner'е, ключ `arbitr:smart_parse_count:<id>`) — `BREAK_SECONDS=1800` (30 мин) пауза, счётчик сбрасывается.
9. На success — `send_parsed_alert(case, new_events, new_files, duration_sec)` шлёт «✅ № · ФИО · N записей, M файлов · Sс» в MAX.
10. `finally`: снимает lock + чистит `arbitr:smart_current_case:<id>`.

### IP-ротация (`ops/arbitr-snat-rotate.sh` + systemd `arbitr-snat-rotate.timer` каждую минуту)
- 4 outbound IP на host eth0 alias: `45.90.35.187`, `31.128.40.116`, `45.12.239.248`, `109.172.47.2`.
- Расписание (МСК): 21–5 → 187, 5–15 → 116, 9–17 → 248, 11–20 → 002. Пик 11–15 = 3 IP активно.
- Скрипт каждую минуту: вычитывает hour, собирает `ACTIVE`-список IP, через `docker inspect` получает source-IP каждого runner-контейнера, ставит **per-runner SNAT**: `iptables -t nat -I POSTROUTING 1 -s <docker_ip> -d <kad_ip> -j SNAT --to-source <assigned_ip>`. Назначение IP runner'ам — по порядку (a=ACTIVE[0], b=ACTIVE[1], c=ACTIVE[2]); если активных < 3 — лишние runner'ы получают пусто в Redis и спят.
- Записывает в Redis (TTL 120с, без Django-префикса `:1:` через `docker exec siricrm-redis-1 redis-cli`):
  - `arbitr:runner_ip:<a/b/c>` — назначенный outbound IP (или пусто)
  - `arbitr:runner_docker_ip:<a/b/c>` — source IP в docker-сети
  - `arbitr:current_snat_ip` — первый активный (legacy)
  - `arbitr:current_snat_active` — csv активных
- Лог: `journalctl -t arbitr-snat -n 20`. Iptables: `iptables -t nat -L POSTROUTING -n -v`.
- 🛑 Контейнеры пишут в Redis из bash напрямую → **БЕЗ префикса `:1:`**. Чтобы task/view увидели — читать через `redis.Redis.from_url(REDIS_URL).get(key)` (не `cache.get(key)`).

### Per-IP captcha cooldown (`apps/arbitr/cooldown.py`)
- Ключ `arbitr:captcha_cooldown_until:<ip>` (TTL 12ч).
- API: `is_active(ip) / until(ip) / activate(ip) -> bool(только_что_активировали) / clear(ip|None) / all_active() -> {ip: dt}`.
- `notifications.handle_captcha(case, page_url, ip)` — дедуплицирует алёрт через `activate()`; если True — `send_captcha_alert(case, page_url, ip)` шлёт в MAX «🚫 IP X в капче до HH:MM, парсинг через другие продолжается».
- Снять вручную: `python manage.py arbitr_clear_cooldown [--ip 1.2.3.4]`. Без флага — все IP.
- 🛑 Раньше cooldown был **глобальным** — капча на одном IP стопила все 3 runner'а. С 2026-06-24 — per-IP.

### Real-time UI на `/arbitr/` (`apps/arbitr/views.py:parser_status`)
- HTMX-полит каждые 5с → партиал `templates/arbitr/partials/_parser_status.html`.
- Верх: бэйдж глобального state (🟢 «Парсит (N/3)» / 🚫 «Все активные IP в капче» / ⚪ «Все runner'ы выключены»).
- Средняя секция: 3 runner-карточки (зелёная если working, серая если disabled), каждая показывает `out_ip`, текущий кейс `№ · ФИО`, счётчик до перерыва `N/8`.
- Правее: статистика 24ч `✅ N ⏸ N ⚠ N 🚫 N`, готовых к парсингу/поиску.
- Последние 3 спарсенных (время + № + ФИО).
- Нижняя строка: 4 IP-чипа с расписанием. Светло-зелёный = активен в этом часу; ярко-зелёный с подписью «Runner X» = используется именно сейчас; красный «🚫 капча до HH:MM» = в cooldown.

### Ловушки
- **Region.arbitr_code ≠ Region.number** — Region.number это код субъекта РФ (Москва=77, Волгоград=34), а kad нумерует свои АС иначе (Москва=А40, Волгоград=А12). См. `apps/crm/migrations/0094_region_arbitr_code_data.py` — заполнено для популярных регионов. Пусто → фильтр выключен в `_search_one`.
- **`_parse_one`/`_search_one` ранние return'ы** (kad_url пусто / ФИО пусто / NotImplementedError) — обязаны ставить `next_*_at = now+24ч`, иначе кейс зацикливается (NULL → снова первый в очереди → снова мгновенный return → ...). Был баг 23.06: 622 error-записи за сутки на одном пустом kad_url кейсе.
- **Утечка Chrome** из `KadSession.__init__` — если spawn упал ДО входа в `with`, `__exit__` не вызывается → zombie Chrome копится → новые драйвера не стартуют. Признак: 6580 PID в контейнере. Лекарство: `docker compose restart arbitr-runner*`. Корневой фикс в `parsers/kad.py:__init__` через try/finally — отложен.
- **`docker exec` в bash с круглыми скобками** — `echo --- (что-то) ---` ломает ssh-парсер. Не использовать скобки в bash echo-сообщениях.

## Ручной запуск + Pause/Resume
- **Ручной запуск** через UI кнопку «Парсить сейчас» → таск `arbitr.kad_monitor_one_case(case_id)` (очередь `arbitr` → runner-a). Читает `arbitr:current_snat_ip` для cooldown-проверки и алёрта. PAUSED → отказ.
- **Pause/Resume** (`case_toggle_pause` view) — флипает PAUSED ↔ (SEARCHING/MONITORING по наличию case_number). Smart-таски фильтруют по статусу, PAUSED выпадает.

## MAX-уведомления

`apps/arbitr/notifications.py`:
- `send_captcha_alert(case, page_url, ip)` — при первой капче на IP, шлёт в `ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID` (env). Дедуп через `cooldown.activate(ip)` — повторные капчи молчат пока активен.
- `send_parsed_alert(case, new_events, new_files, duration_sec)` — после каждого успешного парсинга (даже 0/0). Если будет спамить — добавить gate `if new_events+new_files > 0`.

## Сервисная UI `/arbitr/` — split-layout как чат-модалка

### Общий layout

- **Шапка** (`_layout.html`) — один навбар, 3 зоны:
  - **Слева**: `<img src=logo.png>` + «Сириус-CRM Мониторинг Дел». Ссылка ведёт на `/dashboard/` (главная SiriCRM).
  - **Центр**: поиск с автодополнением (см. ниже) + кнопка-фильтр ⚙. min-width 260px на боковых зонах симметрично, чтобы поиск был визуально по центру.
  - **Справа**: `request.user.username`.
- **Левый sidebar 320px** — список дел, аккордеоны по статусу:
  «🔍 Поиск дела» (янтарный фон), «📋 Мониторинг» (голубой), «⏸ Приостановленные» (серый, свёрнут по умолчанию). Каждая плашка: ФИО клиента (фамилия + инициалы), `📍 регион` услуги, номер дела / 🎯 «найдено дел: N» / 🔍 «ищем…» / ⏸ «пауза», `last_check_at` (date d.m H:i), бейдж state'а ✓/—/⚠/✗.
- **Правая панель `#case-pane`** — содержимое выбранного дела. Подгружается HTMX-swap'ом из `case_detail` view (партиал `_case_pane.html`).

### Active state, deep-link, навигация

- **Active state в sidebar** — delegated JS-handler в `dashboard.html` (toggle класса `.is-active`). Server-side `{% if case.id == selected_case_id %}` хватает только для первого захода — HTMX свапит ТОЛЬКО `#case-pane`. Стиль `.is-active` идентичен `.tg-client-item.active` в чат-модалке: `background-color: var(--ms-blue-light)` + `border-left: 3px solid var(--ms-blue)`.
- **URL deep-link**: `?case=<uuid>` в URL → dashboard сразу инициализирован. `hx-push-url="?case=<uuid>"` на плашках.
- **case_detail view** детектит HTMX: `HX-Request` → партиал `_case_pane.html`; full page → `302 /arbitr/?case=<uuid>`.

### Поиск в шапке (автодополнение)

- `GET /arbitr/search/?q=<text>&groups=<g>&groups=<g>` (`arbitr_search` view) → HTMX-партиал `_search_results.html` с тремя группами:
  - **🔍 В поиске** (`ArbitrCase(SEARCHING)`)
  - **📋 В мониторинге** (`ArbitrCase(MONITORING)`)
  - **📂 Остальные** (`PAUSED`/`CLOSED` + услуги БФЛ без `ArbitrCase`)
- **Нестрогий поиск по токенам**: `q.split()` по пробелам, КАЖДОЕ слово должно матчить ХОТЯ БЫ ОДНО поле (`last_name` / `first_name` / `patronymic` / `case_number` / `region.name`) — AND-of-OR. Так «Иванов Иван» находит клиента с last_name=«Иванов» first_name=«Иван».
- **Лимит 20 на группу**, сортировка `-last_check_at` (для ArbitrCase) или `-created_at` (для Service).
- Минимум **2 символа** для показа dropdown'а; CSS `#search-results:empty { display: none; }` прячет пустой.
- **Фильтр групп** — popover ⚙ с тремя чекбоксами (все включены по умолчанию). При toggle search re-fires через `htmx.trigger(input, 'input')`. `hx-include="#arbitr-filter-form input:checked"` подтягивает activated groups в query как `?groups=searching&groups=monitoring` (view → `request.GET.getlist('groups')`).
- **Строка результата** (`_search_row_case.html` / `_search_row_service.html`):
  - Слева click-zone: ФИО · 📍 регион · номер дела · `📌 общий статус услуги` (`Service.common_status.name` из канбана). Клик → грузит дело в `#case-pane` + закрывает dropdown (`arbitrCloseSearch()`).
  - Справа кнопки (`hx-on:click="event.stopPropagation()"` чтобы не триггерить click-zone):
    - SEARCHING/MONITORING → ⏸ Пауза
    - PAUSED → ▶ Возобновить
    - Service без дела → + На мониторинг (`case_create_searching` view создаёт ArbitrCase(SEARCHING))

### Ручной парсинг — прогресс + abort + автообновление

🛑 **Активный таск отслеживается через Redis-кэш** (`arbitr:active_task:<case_id>`, TTL 10 мин). Значение: `{task_id, started_at, user}`. View `case_run` сохраняет; `tasks.kad_monitor_one_case` в `finally` блоке очищает (сигнал «таск завершился»).

- **`POST /case/<uuid>/run/`** → `kad_monitor_one_case.delay()` → cache.set → возврат карточки с прогресс-UI. Если кэш уже занят → 409 Conflict (защита от двойного клика).
- **Прогресс-UI** в `_case_card.html` (когда `case.active_task` есть):
  - Голубой alert: spinner + «Парсинг идёт {N}с · запустил {user}» + красная кнопка «✕ Прервать».
  - Кнопки «Пауза»/«Парсить сейчас» заменены на «…ждём завершения».
- **Self-poll**: карточка добавляет `hx-get=/case/<id>/card/?polling=1 hx-trigger="every 3s"` на себя пока активна. Сервер на каждом poll:
  - Кэш ещё есть → возврат свежая карточка с обновлённым `elapsed`.
  - Кэш очищен (таск завершился) → 204 + `HX-Refresh: true` → браузер делает **full reload** (sidebar, события, лог, счётчики — всё обновляется).
- **Abort**: `POST /case/<uuid>/abort/` → `AsyncResult(task_id).revoke(terminate=True, SIGTERM)` → cache.delete → ArbitrCheckLog(ERROR) → `HX-Refresh: true`.

### Прочее

- **HTMX-поллинг лога** раз в 4с пока последний `ArbitrCheckLog.ts` моложе 60с (`_log_is_fresh`).
- Меню — `MenuItem(section='Арбитраж', name='Мониторинг дел', url='/arbitr/', icon='landmark')`, миграция `core.0016_arbitr_menu_item` + `0017_arbitr_menu_attach_to_configs` (см. ловушку ниже).
- **Шаблоны используют `{% load humanize %}`** — `django.contrib.humanize` в `INSTALLED_APPS`.
- 🛑 **Arbitrary tailwind-классы НЕ работают** (`grid-cols-[320px_1fr]`, `h-[calc(...)]`) — pre-compiled tailwind.css без JIT. Использовать inline `style="display:grid; grid-template-columns:..."`.
- 🛑 **Django template engine парсит `{% %}` ВЕЗДЕ**, включая CSS/JS-комментарии — `{% if %}` в `/* комменте */` сломает парсинг.

## Поиск по ФИО — поле «Участник дела» + JS-click

🛑 На главной kad **6 полей фильтра** — каждое для своего scope:
- `textarea[placeholder="название, ИНН или ОГРН"]` → **поиск по сторонам** (для ФИО клиента или организации). Константа `KadSession.FIELD_PARTY`.
- `input[placeholder="например, А50-5568/08"]` → поиск по номеру дела (`FIELD_CASE_NUMBER`).
- `input[placeholder="фамилия судьи"]` (`FIELD_JUDGE`), `input[placeholder="название суда"]` (`FIELD_COURT`), 2× даты.

ФИО клиента надо лить в **textarea «Участник»**, не в input «По делу». В обратном случае kad на ФИО в input «По делу» возвращает «Укажите атрибуты дела для поиска» → парсер всегда returns nothing. По умолчанию `_submit_search_form(query, field=FIELD_PARTY)`.

🛑 При вводе в textarea Участник kad показывает suggester-dropdown (POST `/Suggest/CaseNum`), который перекрывает кнопку «Найти» через z-index → `selenium.click()` → `ElementClickInterceptedException`. **Лечение**: `driver.execute_script('arguments[0].click()', btn)` (JS-click игнорирует z-index overlap).

## Confirm-flow: SEARCHING → MONITORING через UI

- `_search_one` пишет `KadSearchHit[]` в `ArbitrCase.search_hits` (JSONField) + `search_hits_at` (DateTime).
- `_case_pane.html` для status=searching рендерит блок «🎯 Найденные кандидаты»: каждый hit (`case_number`, `court_name`, `parties`, `filed_at`) + ссылка `kad ↗` (target=_blank) + кнопка «✓ Это моё дело» (form POST → `/arbitr/case/<uuid>/confirm-hit/<int:index>/`).
- `views.case_confirm_hit` валидирует индекс, выставляет `case_number/kad_url/court_name` из hit'а, переводит case в MONITORING, очищает search_hits, пишет `ClientEvent` + `ArbitrCheckLog`. HX-Redirect (для HTMX) / 302 (full submit) → reload dashboard'а с `?case=<uuid>` → sidebar обновится со сменой статуса.

## URL-эндпоинты (`apps/arbitr/urls.py`)

```
/arbitr/                                       dashboard           # split-layout (sidebar + #case-pane)
/arbitr/search/?q=&groups=                      arbitr_search       # HTMX-партиал автодополнения
/arbitr/case/<uuid>/                            case_detail         # HTMX → _case_pane.html; full → 302 ?case=
/arbitr/case/<uuid>/card/?polling=1             case_card_partial   # карточка для swap'а + self-polling
/arbitr/case/<uuid>/log/                        case_log_partial    # лог проверок (self-polling раз в 4с)
/arbitr/case/<uuid>/run/                        case_run            # POST: ставит таск в очередь + cache.set
/arbitr/case/<uuid>/abort/                      case_run_abort      # POST: revoke task + cache.delete + HX-Refresh
/arbitr/case/<uuid>/toggle-pause/               case_toggle_pause   # POST: PAUSED ↔ SEARCHING/MONITORING
/arbitr/case/<uuid>/confirm-hit/<int>/          case_confirm_hit    # POST: SEARCHING → MONITORING из hit[idx]
/arbitr/service/<uuid>/start-search/            case_create_searching # POST: создать ArbitrCase(SEARCHING) для услуги
```

Legacy (старое — карточка услуги в канбане):
```
/arbitr/service/<uuid>/iskotpravlen/            mark_iskotpravlen
/arbitr/case/<uuid>/confirm/                    confirm_case        # старый confirm с вводом case_number/kad_url
/arbitr/service/<uuid>/case-block/              case_block          # партиал для карточки услуги
```

> ⚠ **Ловушка для новых пунктов sidebar**: `context_processors.sidebar_menu` рендерит **не все** `MenuItem`, а только привязанные к `DashboardConfig.menu_items` (M2M) у конфига сотрудника. Создания `MenuItem.objects.create(...)` **недостаточно** — нужно ещё пройтись по `DashboardConfig.objects.all()` и сделать `.menu_items.add(item)`. См. `0017_arbitr_menu_attach_to_configs` как образец.

## Команды отладки

```bash
# открыть главную (быстрый чек что Chrome+Xvfb+stealth поднимаются)
docker exec siricrm-arbitr-runner-1 python manage.py kad_probe open
# поиск через UI (с прогревом)
docker exec siricrm-arbitr-runner-1 python manage.py kad_probe search "А12-33291/2024"
docker exec siricrm-arbitr-runner-1 python manage.py kad_probe search "Иванов Иван Иванович" --court А12
# парсинг карточки
docker exec siricrm-arbitr-runner-1 python manage.py kad_probe case https://kad.arbitr.ru/Card/<uuid>
```

## Env-vars

- `ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID` — куда слать алёрт о капче (один на всех).
- `ARBITR_HEADLESS=true|false` — для локальной отладки парсера без Xvfb (по умолчанию `true` — но Chrome в нашем контейнере фактически headed через Xvfb, флаг управляет только `--headless=new` arg).

## Скачивание PDF — особая защита kad

Endpoint `/Kad/PdfDocument/<case>/<doc>/<filename>.pdf` нельзя дёрнуть напрямую — kad отдаёт **ПравоКапчу** (`<form id="tokenFrom">` + JS `pravocaptcha.execute`). Эта защита не снимается ни правильным `Referer`'ом через CDP, ни UA, ни прогретой сессией с куки. **Срабатывает только legitimate-flow**: open card → click on PDF link (= same JS-context, same opener).

Поэтому `KadSession.download_pdf(url, *, referer=card_url)`:
1. Если карточка дела (`referer`) ещё не открыта — `driver.get(referer)`
2. `window.open(pdf_url, '_blank')` — новая вкладка с тем же browsing-context, opener = карточка. kad доверяет такому запросу.
3. Chrome скачивает PDF на диск (Chrome prefs `plugins.always_open_pdf_externally=True` + `download.default_directory=/tmp/arbitr_dl_<uuid>/`, дублирован через CDP `Page.setDownloadBehavior`).
4. Ждём появления файла без `.crdownload` → читаем bytes → удаляем → закрываем «лишние» вкладки (в `finally`, чтоб подчистить и при captcha/error).

`_download_new_attachments(kad, case)` пробегает все `ArbitrAttachment.stored_file IS NULL` → `download_pdf(att.kad_url, referer=case.kad_url)` → `upload_file_to_s3(prefix='arbitr/<case_id>')` → `StoredFile` → `att.stored_file = stored`. Best-effort: ошибки отдельного файла не валят батч, capcha — пробрасываем (батч остановится, MAX-alert).

## Прямой POST `/Kad/SearchInstances` через `requests` или `$.ajax` всегда 451

⚠ **Это нормальное поведение kad, НЕ IP-блок.** kad детектит запрос «не из браузер-контекста» (нет правильного fingerprint'а / нет легитимной цепочки JS-событий) и отдаёт 451 «Доступ заблокирован». Прямой GET `/Card/<uuid>` без UI-flow тоже даёт ПравоКапчу 3.4КБ.

Нельзя по этим 451/captcha-ответам делать вывод что наш IP в blacklist'е — это анти-бот по поведению клиента, не по IP. **Правильный путь — UI-flow через Chrome**: открыть главную → ввести в input → click `[alt="Найти"]` → kad сам шлёт XHR из своей JS-сессии.

Раньше в этом доке была неверная диагностика (за 10.06.2026 — «IP dev в blacklist'е», новые UI-селекторы tag-input/`#b-form-submit`, residential proxy как решение). Откатил 11.06.2026 после правильной диагностики (виновник = PDF prefs, см. выше). Прокси/VPN-обход НЕ нужен.

## Известные ограничения

- **PDF flow не оттестирован end-to-end** — архитектура через `window.open` + `Chrome download prefs` готова, на тестовом MONITORING-кейсе А12-33291/2024 парсер собрал 170 событий и 46 attachments за 79с, но IP попал в kad blacklist (см. выше) и финальный прогон с PDF не получилось сделать. Когда репутация откиснет — `docker exec siricrm-arbitr-runner-1 python manage.py pdf_diag --referer <card_url> <pdf_url>` должен вернуть `OK: ct='application/pdf' bytes=… magic=b'%PDF-'`.

## Deploy на prod (apps/arbitr)

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
