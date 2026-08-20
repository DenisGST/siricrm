# todo — что осталось / куда вернуться

Живой список незавершённого. Каждый пункт: **что уже сделано** · **что осталось** · **файлы/команды**.

Метки: 🔴 надо сейчас · 🟡 ближайшее время · 🟢 при случае · ⏸ ждёт внешнего (креды/договор/решение).

Ревизия: **20.08.2026** (предыдущая — 06.07.2026).

---

# 🔴 Новые баги

_Раздел для свежих находок. Формат: симптом → где воспроизводится → статус._

| # | Симптом | Где | Статус |
|---|---|---|---|
| 1 | **Оплата с сайта fo-y.ru не проходит** — посетитель жмёт «Оплатить» по 5–10 раз, платежа нет | fo-y.ru + [apps/accounting](apps/accounting/) | 🟠 CRM-часть готова, **ждёт правки сайта** — см. ниже |
| 2 | **Инъекция стороннего JS на fo-y.ru** (`sflog.ru` → `datacdn.ru`, WebSocket-канал управления) на всех страницах, включая оплату | fo-y.ru (Joomla) | 🔴 подтверждена, **лечится на стороне сайта** — [разбор](docs/foy-injection-2026-08.md) |

## 🔴 №1 — Оплата с сайта fo-y.ru

**Причина (найдена 20.08).** Страница оплаты грузила JS-виджет
`securepay.tinkoff.ru/html/payForm/js/tinkoff_v2.js`, а этот хост отдаёт сертификат
**Russian Trusted Sub CA (Минцифры)**. Браузер без этого корня блокирует скрипт
(`ERR_CERT_AUTHORITY_INVALID`) → `window.pay` = `undefined` → клик падает с
`ReferenceError: pay is not defined` **молча**. Beacon `prepay` уходил до `pay()`,
поэтому в CRM попытки копились, а оплат не было.

🛑 **К переезду dev отношения не имеет** — обвал начался ~03.08, переезд был 09.08.
Цифры прода: 17.08 — 105 попыток от 5 человек и 2 оплаты; в июле было 3 попытки → 3 оплаты.
Платят только те, у кого корень Минцифры установлен (Яндекс.Браузер, приложение ТБанка).

**Сделано (в репозитории, на dev проверено реальным Chrome):**
- `integrations.acquiring_init()` — серверный Init через `securepay.tbank.ru` (Let's Encrypt).
- `views.acquiring_pay` + `POST /accounting/acquiring/pay/` — сохраняет prepay, делает Init,
  отдаёт `302` на `pay.tbank.ru`. Ошибка → `templates/accounting/pay_error.html`.
- Дефолт `TBANK_ACQUIRING_API_BASE` → `https://securepay.tbank.ru/v2`.

**Осталось:**
1. 🔴 Выкатить на прод (`deploy` — новых зависимостей нет).
2. 🔴 **Поправить страницу fo-y.ru** (Joomla, вне репозитория) — готовый блок:
   [docs/foy-payment-form.html](docs/foy-payment-form.html). Пока сайт не поправлен,
   оплата не заработает.
3. 🟢 Эндпоинт публичный и создаёт Init кому угодно (как и прежняя форма) —
   при случае добавить троттлинг по IP.

## 🔴 №2 — Инъекция стороннего JS на fo-y.ru

**Полный разбор:** [docs/foy-injection-2026-08.md](docs/foy-injection-2026-08.md).

Кратко: в `<head>` всех страниц, между счётчиками GTM и Top.Mail.Ru, вставлен
`<script src="data:text/javascript;base64,…">` — пятислойно замаскированный загрузчик
(`data:` URI → base64 → HTML-сущность `&#x67;` внутри base64 → `\uXXXX` + склейка строк →
самоудаление узла). Тянет `sflog.ru` → `cdnsec.ru/rules.js` + `datacdn.ru` (WebSocket-клиент
Iris + localForage = **постоянный канал управления в браузере посетителя**). Отключает себя
на песочницах `jshell.net`/`appspot`, запускается по scroll/mousemove, глушит чужие скрипты.

🛑 Сейчас цепочка оборвана **случайно** — у `sflog.ru` протух сертификат 18.08.2026.
Перевыпустят — заработает снова.

Отдельно: в CSS-классе пункта меню «Главная» лежит неудавшийся XSS
(`"><script src="https://js.aacaw.com/fp/v1.min.js"></script>`) — Joomla его экранирует,
не выполняется, но это индикатор доступа к админке.

**Осталось (всё на стороне сайта, доступа у нас нет):** снести загрузчик, очистить класс
пункта меню, сменить все пароли, проверить Super User'ов и другие закладки, обновить Joomla
и SP Page Builder / Helix Ultimate, перепроверить. Наши `siricrm.ru`/`crmsiri.ru` — чисто.

---

# Инфраструктура

## 🔴 Погасить старый dev `5.35.94.218` — срок вышел

**Контекст:** dev переехал на `213.155.29.31` (09.08.2026), старый сервер оставлен резервом
«до ~16.08.2026». Сегодня 20.08 — просрочено.

**Осталось:**
1. Убедиться, что на старом ничего не осталось (VPN погашен, контейнеры остановлены — проверить `ssh siri-dev-old`).
2. Погасить/удалить площадку у провайдера.
3. Почистить хардкод `5.35.94.218` в `ALLOWED_HOSTS` — [config/settings/prod.py](config/settings/prod.py)
   (актуальный dev-IP приходит из `.env.dev`, правка кода не нужна).
4. Убрать алиас `siri-dev-old` из `~/.ssh/config` на рабочей машине и на проде.

## 🟡 Ключ нового dev не добавлен в `authorized_keys` прода

`ssh siri-prod` с `213.155.29.31` не работает. Деплой/мониторинг не затронуты (идут по HTTPS
через DevOps-агента), но прямая диагностика прода с dev недоступна.
**Осталось:** скопировать `/root/.ssh/id_ed25519.pub` (комментарий `siri-dev-new`) в `authorized_keys` прода.

## 🟢 Прод: адрес `10.8.1.4` дублируется у `awg1` и `claude0`

Латентная двусмысленность в таблице `local` (проверено 03.08 — failover не ломается,
`SO_BINDTODEVICE` спасает). **Осталось:** попросить провайдера перевыпустить прод-конфиг с другим адресом.

## 🟢 Прод-backup на NAS — disaster recovery

Память [`prod-backup-strategy-pending`](/root/.claude/projects/-var-www-siricrm/memory/prod-backup-strategy-pending.md).
Два варианта: `rclone+rsync` vs `restic`. Решение откладывается.

---

# Telegram

## 🟡 Лиды с лендинга не принимаются — нужен второй бот

**Что сейчас:** `poll-telegram-leads` **выключен на обоих серверах**, лид-канал не настроен,
TG-лидов на проде 0. Личку (коды привязки) ловит отдельная beat-задача `telegram.poll_bot_private`.
Прод — @SiriusCRMBot, dev — свой бот (он же бот мониторинга). Код везде читает единый
`TELEGRAM_BOT_TOKEN` ([apps/telegram/leads_bot.py:28](apps/telegram/leads_bot.py#L28),
[apps/core/tasks.py](apps/core/tasks.py), [apps/telegram/bot_sender.py:26](apps/telegram/bot_sender.py#L26)).

**Осталось (если лиды снова понадобятся):**
1. Отдельный токен для лид-бота от BotFather → `TELEGRAM_LEADS_BOT_TOKEN`
   (🛑 один токен = один поллер, `getUpdates` с двух мест даёт 409).
2. Развести токены в коде: `leads_bot.py` → `TELEGRAM_LEADS_BOT_TOKEN`;
   `core/tasks.py` (`poll_monitor_bot`/`monitor_health`/`monitor_vpn`/`daily_health_report`) → `TELEGRAM_MONITOR_BOT_TOKEN`;
   [setup_telegram_leads_webhook.py](apps/telegram/management/commands/setup_telegram_leads_webhook.py).
3. Решить webhook vs polling. 🛑 Webhook на наших серверах **не работает** — split-tunnel заворачивает
   ответный SYN-ACK в туннель. Если пробовать webhook — сначала эксперимент на dev
   (`getWebhookInfo.last_error_message`), при `timeout` нужен policy routing (CONNMARK + ip rule).
   Безопасный путь — просто включить `poll-telegram-leads` (его `_poll_once` умеет и лиды, и привязку).
4. ENV прода: `TELEGRAM_LEADS_CHANNEL_ID=-1003960014349`, `TELEGRAM_LEADS_WEBHOOK_SECRET` (уже есть в `.env.dev`).
5. Cleanup: DNS A-запись `telegram.siricrm.ru` (не нужна, если polling).

---

# MAX

## 🟡 Пересланные файлы не скачиваются

**Сделано (26.06):** вложение без `url` («Переслано: …» из другого мессенджера/почты) больше не
теряется молча — видимый плейсхолдер + полный payload в `raw_payload.body`.

**Осталось:** взять сэмпл `Message.objects.filter(channel="max", raw_payload__unhandled_attachment=True)`
→ разобрать `body` → добавить парсинг нестандартной структуры → реальное скачивание.

**Файлы:** [apps/maxchat/processing.py](apps/maxchat/processing.py).

---

# Процедуры БФЛ / АФД

## 🟡 Почта России, этап 2 — Otpravka API + трекинг РПО

**Сделано (20.07, на проде):** выгрузка .xlsx в формате файла загрузки заказов ЛК
`otpravka.pochta.ru` — запросы + кредиторы + должник, построчный вес и уведомление,
`INDEXFROM` из `ArbitrationManager.ops_index`. Конверты/Ф103 печатает сам ЛК.

**Осталось:**
- **Вариант А:** Otpravka API (`/1.0/clean/address`, `/1.0/user/backlog`, `/1.0/user/shipment`,
  печатные формы `/f7p`, `/f103`) + Tracking API (SOAP, отдельные креды) — факт вручения.
- **Вариант Б:** ЭЗП (`/1.0/erl/send`) — требует УКЭП и `.sig` на каждое письмо.
- 🛑 У Почты **три разных договора/авторизации** (Otpravka / Tracking / ЭЗП), договор — на АУ. ⏸

**Файлы:** [apps/procedure/pochta_export.py](apps/procedure/pochta_export.py).

## 🟡 Отправка запросов по email + контроль ответов по времени

`sent_method` есть, реальной отправки нет (SMTP не настроен). Контроль просрочки уже работает
(beat `procedure.mark_overdue_requests` → событийка `request_overdue`), не хватает самой отправки
и автоматической фиксации ответа.
**Задел:** транспорт с личного ящика уже написан для Коммерсанта — [apps/kommersant/mailer.py](apps/kommersant/mailer.py),
креды в профиле (`Employee.kommersant_email`), можно переиспользовать.

## 🟢 Запросы в госорганы: пробелы справочников

1. **ЦЗН (центры занятости)** — вида нет в справочнике, `req_employment` на ручном вводе.
   Районного уровня (сотни на страну) → источник трудвсем/открытые данные, не DaData.
2. **Гостехнадзор — 18 регионов без органа** (вкл. Волгоград): в ЕГРЮЛ названы иначе, DaData не находит.
   Заполняются вручную из модалки и запоминаются `RecipientRule`.
3. **153 МРЭО без индекса** — DaData не геокодировала общие адреса, индекс на конверте пустой.
4. **Чечня/новые территории** — гочча KLADR-95 (`Region.number=95`=Чечня, KLADR-95=Херсон):
   ОСФР/гостехнадзор не заведены.

**Команды:** `import_osfr`, `import_gostehnadzor`, `enrich_legal_entity_postal_code --kinds МРЭО`,
`wire_recipient_kinds`. 🛑 DaData гонять только на dev (квота 10k/день), потом переносить dev→prod.

## 🟢 Справочник АУ на проде пуст

Без `ArbitrationManager` с PNG-подписью AFD-генерация запросов даёт пустой блок ФУ
(предпроверка подсветит красным), а в выгрузке Почты пустая колонка `INDEXFROM`.
**Осталось:** завести АУ в Справочниках прода (ФИО/ИНН/СНИЛС/СРО/`ops_index`/`signature_file`). ⏸ ждёт данных от АУ.

## 🟢 DRAFT-каталог мероприятий — подтвердить с АУ

`MilestoneTemplate` засеян черновиком (`procedure_seed`), сроки не выверены. Правится в
Справочниках → «Шаблоны мероприятий». ⏸

## 🟢 Ответ на запрос из реестра не подшивается в файл-менеджер

Из трёх точек входа ответа две (Входящие, модуль сканера) кладут файл и в `Request.response_scan`,
и в папку клиента, а путь «📥 в реестре» ([request_response](apps/procedure/views.py)) — только
в `response_scan`. **Осталось:** решить с юристами, ожидаемо ли это; если нет — выровнять поведение.

---

# ЕФРСБ

## ⏸ Read-API спит — нет кредов

**Работает уже сейчас:** каталог типов/подтипов, генерация текста сообщения, копирование в ЛК,
.docx/.pdf в папку «Публикации ЕФРСБ».
**Спит:** `EFRSB_ENABLED=False`, кредов нет → `is_configured()=False`, кнопки проверки/мониторинга
скрыты, beat `efrsb.monitor_active_cases` не даёт эффекта.
**Осталось:** получить доступ к read-API оператора → включить `EFRSB_ENABLED`/`EFRSB_MONITOR_ENABLED`,
проверить `check_publication` на боевом контуре.
🛑 Публикационного API у fedresurs нет и не будет — текст всегда вставляется в ЛК вручную.

## 🟢 Разметка `is_bfl` / `applicable_kinds` — DRAFT

35 типов и 18 подтипов размечены на глаз, подтвердить с АУ. Правится в админке/Справочниках,
`efrsb_seed` правки не затирает.

---

# КоммерсантЪ

## ⏸ Вживую не тестировалось — нет почтовых кредов АУ

Код, Celery-задачи, UI, предпроверки, IMAP-приём счёта работают; реального обмена с
`pb@kommersant.ru` не было.
**Осталось:** креды ящика АУ в профиле (`role="arbitration"`) → первую заявку отправить под присмотром.
Дополнительно: факсимиле на dev не проверить (подпись в прод-бакете S3), формулировки шаблонов — DRAFT,
бланк только для ФЛ (для ЮЛ нужен `blank-company`), «Ъ-Публикатор» — когда ИД пришлёт спеку.

---

# Бухгалтерия (ТБанк)

## 🟡 Уведомления о платежах + событийка

При новом входящем платеже → уведомление бухгалтеру (`apps/notifications`); при привязке →
`record_action` в логе клиента + уведомление ответственному/исполнителю.
**Файлы:** [apps/accounting/services.py](apps/accounting/services.py), [apps/crm/client_log.py](apps/crm/client_log.py).

## 🟡 Оплата с сайта — гарантировать привязку к клиенту

Сейчас ФИО/телефон вводит клиент вручную (опечатки) → разнесение вручную.
**Осталось:** авто-матч по телефону (`find_client_by_phone`) на prepay/вебхуке + предзаполнение
клиента в модалке привязки; в идеале — генерить платёжную ссылку из CRM с зашитым
`OrderId=client/service` (тогда привязка автоматическая).

## 🟢 Оплата через мессенджеры

Кнопка в чат-панели → `Init` с `OrderId=client/service` → ссылка в TG/WA/MAX через существующие
sender'ы → нотификация `CONFIRMED` → авто-привязка по `OrderId`.

## 🟢 UI бухгалтера — доработать

Поиск по сумме/ФИО/назначению/контрагенту (сейчас заглушка), фильтры период/сумма,
архив обработанных, отдельная работа с неопознанными. Опц. — вкладка «Банк» под ручной импорт выписок.

---

# Уведомления

## 🟢 Stage C — дублирование уведомлений в Telegram

Зеркало каждого уведомления сообщением с inline-кнопками; callback → та же `services.respond`
(`via="telegram"`), `tg_message_id` для синхронной правки карточки.
🛑 Нужен **отдельный** токен (`NOTIFY_BOT_TOKEN`) — иначе конфликт `getUpdates` с leads/monitor-ботами
(см. тот же затык, что в разделе Telegram выше).
Опц.: вынести `notify()` в Celery (сейчас inline в запросе).

---

# Данные / чистка

## 🟢 Дедуп задвоенных папок файл-менеджера

**Сделано:** `_mk` устойчив к дублям — берёт старейшую подходящую папку (коммит `1797a56`, на проде),
генерация договоров/заявлений и привязка сканов работают.
**Осталось (очистка, не срочно):** у ~118 клиентов задвоены дефолтные `ClientFolder`
(`root` ×118; `chat`/`chat_sent`/`chat_received`/`personal` ×117; ~900 дублей с пустым slug).
Нужна идемпотентная команда `dedupe_client_folders` с `--dry-run`: перенести `ClientFile` и `children`
в старейшую папку по (`client`+`slug`), удалить опустевшие.
**Файлы:** [apps/files/folder_utils.py](apps/files/folder_utils.py), новая
`apps/files/management/commands/dedupe_client_folders.py`.
Память: [`duplicate-client-folders`](/root/.claude/projects/-var-www-siricrm/memory/duplicate-client-folders.md).

## 🟢 `map_gosorgan_to_legalentities` — добить fuzzy

Покрытие 575 LE с `bubble_id` из 1862 Gosorgan; **1190 unmatched** — имена в Bubble отличаются
от официальных. Нужен token-set fuzzy.
**Файлы:** [apps/procedure/management/commands/map_gosorgan_to_legalentities.py](apps/procedure/management/commands/map_gosorgan_to_legalentities.py).

## 🟢 Bubble `apply_messagewsp` игнорирует `url_file`

Известный дефект: медиа качается только из `body`, а это подписанная ссылка `*.cdn.bubble.io`,
которая протухает в 403. На апрельской доливке дало 189 несклеенных вложений (152 починены
перезапросом Data API).
**Осталось:** в applier'е брать `url_file` первым, `body` — фолбэком, чтобы будущие дельты не теряли медиа.
**Файлы:** [apps/bubble_import/appliers.py](apps/bubble_import/appliers.py).

---

## Что НЕ входит в этот файл

- Чужие WIP в working tree — задачи других сессий, ведут их авторы.
- Userbot ([apps/telegram/userbot.py](apps/telegram/userbot.py)) — явное указание «не трогать».
- Арбитраж на dev не работает — это **ожидаемо** (мульти-раннерная схема только на проде), не баг.

---

## Недавно закрыто (для истории, чтоб не искать)

| Дата | Что | Как решилось |
|---|---|---|
| 20.08 | Bubble: дельта за август не была залита | `fetch_bubble_since 2026-08-01 --by created` + точечный apply только `pending` → 301 запись (Files 146, Корреспонденция 129, Money 25, Man 1), 0 ошибок. Проверка: все 368 августовских записей `imported` |
| 09.08 | Переезд dev `5.35.94.218` → `213.155.29.31` | База с прода (234 МБ, 1:41), VPN, DNS, сертификат. Площадка-кандидат `5.180.21.126` отбракована — нет маршрутов до РФ в обе стороны |
| 04.08 | Дыра в истории WhatsApp (апрель–май 2026) | Доливка 5 897 сообщений, расхождение с Bubble = 0. Не прошло 11 (номера не в базе / пустые) |
| 04.08 | Второй IP арбитража `45.12.73.96` на dev | Удалён — правило SNAT показывало `0 pkts`, адрес не был в netplan |
| 02.08 | `Service.visible_to` не зеркалил `Client.visible_to` | Добавлены `sees_all_clients`/`is_owner`/`managing_partner` — карточка процедуры перестала давать 404 юристу |
| 20.07 | Выгрузка для Почты РФ (файл загрузки заказов ЛК) | На проде. 🛑 Ф103 печатает ЛК, не мы |
| 20.07 | Раннеры арбитража несли старый отозванный `TELEGRAM_BOT_TOKEN` | `up -d --force-recreate` четырёх раннеров (🛑 `restart` env не перечитывает) |
| 06.07 | Кнопка `disk_usage` в DevOps-дашборде | Кнопка «💾 Диск» есть в [templates/devops/dashboard.html:126](templates/devops/dashboard.html#L126) |
| 06.07 | Zombie-процессы (web=20, celery=48) | `init: true` в compose → tini как PID 1, после `--force-recreate` на проде 0 zombie |
| 06.07 | Логи celery: transient 502 при deploy + TG long-poll timeout как ERROR | Коммит `94ee55a` — понижено до INFO с прицельным catch |
| 30.06 | VPN на проде: единственный канал `claude0` | Установлен `amneziawg`, поднят `awg1`, `awg-failover.service` активен |
| 25.06 | Send-таски не помечали `is_failed` при exhaustion | Коммит `a219268` — `is_failed` в `MaxRetriesExceededError` для WA/MAX/TG |
| 24.06 | DNS-фолбэк на проде | `/etc/resolv.conf` 3 nameserver + compose `dns:` через YAML anchor |
