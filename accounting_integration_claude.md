# Бухгалтерский учёт — рабочее место бухгалтера + интеграция ТБанк (`apps/accounting`)

Раздел «Бухгалтерский учёт» (`/accounting/`) — рабочее место бухгалтера. Грузится в
`#content-area` сайдбар-пунктом (`use_htmx`), повторяет chrome главной. Две интеграции
с ТБанк (выписка р/с + эквайринг) + ручное разнесение входящих платежей по клиентам.

## Доступ

`apps/accounting/permissions.py:can_access_accounting` — суперюзер, роль `accountant`/`admin`,
руководство (через `is_references_access`). Гейт пункта меню — спецкейс в
`apps/core/context_processors.py` (по url `/accounting/`, как у «Сканов»), гейт вьюх —
декоратор `require_accounting`. 🛑 У бухгалтера должна быть роль `accountant` (или выше).

## UI — 3 вкладки (`templates/accounting/`)

`panel.html` (оболочка: заголовок + специфичный поиск-заглушка + табы daisyUI `tabs-boxed`,
табы грузятся партиалами по HTMX в `#acct-tab`):

1. **Банк** (`_tab_bank.html`) — мониторинг двух источников (карточки `_bank_source_card.html`):
   выписка = поллинг (последняя/следующая проверка + «Проверить сейчас»), эквайринг = вебхук
   (показывает NotificationURL). + сводка очереди + история `SourcePoll`. Самообновление по
   событию `acctBankChanged`.
2. **Уведомления** (`_tab_notifications.html`) — очередь разнесения. Фильтры: статус
   (Не привязаны/Неопознанные/Привязаны/Все) · тип (Все/Прямые/Эквайринг-зачисления) ·
   источник (Р/с/Эквайринг). Клик по строке → платёжка `_payment_detail.html` (read-only,
   данные из выписки). Кнопки: «Привязать» (модалка) / «Неопознанный». 🛑 У эквайринг-зачислений
   (`is_settlement`) кнопки «Привязать» нет, строка серая. Самообновление по `acctQueueChanged`.
3. **Внесённые платежи** (`_tab_payments.html`) — реестр `finance.Payment(direction=in)`.

**Модалка привязки** (`_bind_modal.html` + `_bind_clientblock.html` + `_bind_client_results.html`):
поиск клиента (многословный: каждое слово по всем полям через OR, между словами AND — «Иванов
Иван» сужает), в результатах ФИО · регион (`Service.region`) · `услуга: статус` · телефон.
Выбор клиента → начисления клиента (`Charge` с остатком, жадное предзаполнение суммы) →
распределение (можно разбить на несколько, JS-счётчик «распределено/остаток») → параметры
платежа (тип дохода/счёт/форма/дата, авто-дефолт по источнику, редактируемо). 🛑 Модалка через
`.showModal()`, скрипт в IIFE; `hx-on::after-request` закрывает модалку только на POST-сабмите
(`requestConfig.verb === 'post'`), иначе поиск клиента (hx-get внутри формы) её схлопывал.

## Модели (`apps/accounting/models.py`)

- **`IncomingPayment`** — буфер входящих. `source` (statement/acquiring), `external_id`
  (uniq с source — дедуп: operationId выписки / PaymentId эквайринга), `occurred_at`, `amount`,
  `payer_name`/`payer_inn`/`payer_phone`, `purpose`, `order_id`, `status`
  (new/bound/unidentified), `is_settlement` (эквайринг-зачисление в выписке), `note`, `raw`
  (JSON сырой операции/нотификации), `bound_client`/`bound_by`/`bound_at`,
  `created_payments` (M2M → finance.Payment).
- **`SourcePoll`** — журнал опросов/нотификаций (для вкладки «Банк»).
- **`AcquiringPrepay`** — данные со страницы оплаты ДО платежа (order_id uniq → name/phone/amount,
  matched). Нужна т.к. ТБанк не возвращает введённые ФИО/телефон (см. ниже).

## Ручное разнесение (`services.py`)

`bind_incoming_payment(ip, employee, client, allocations, income_type, incoming_account,
payment_form, payment_date, note)` — создаёт `finance.Payment` по одному на начисление
(`allocations` = [(Charge|None, amount)]; пусто → один Payment на всю сумму без начисления;
сумму можно разбить). `Charge.paid_amount` авто-суммирует → начисление гасится. Повторная
привязка («Изменить») сначала откатывает прежние Payment. `mark_unidentified` — статус
«неопознанный» (бухгалтер не смог определить плательщика). Дефолты: счёт по источнику
(`default_incoming_account`: эквайринг → «Эквайринг», иначе «Расчётный счёт»/«Тинькофф»),
тип дохода `default_income_type` («Оплата юруслуг»).

## Источник 1 — Выписка р/с (T-API «Бизнес», pull)

`integrations.fetch_incoming("statement", since)`: `GET https://business.tbank.ru/openapi/api/v1/statement`,
заголовок `Authorization: Bearer {TBANK_BUSINESS_API_TOKEN}` + `X-Request-Id`. Параметры:
`accountNumber`(20 цифр), `from`(ISO8601), `operationStatus=Transaction`, `limit`(≤5000), `cursor`.
Ответ `{operations:[...], nextCursor}`.

🛑 **РЕАЛЬНЫЕ имена полей операции** (подтверждены на live): `operationId`(UUID→external_id),
`operationDate`(ISO→occurred_at), `typeOfOperation`(=="Credit" → входящий), `operationStatus`
(=="Transaction" проведён, не холд), `accountAmount`(сумма В РУБЛЯХ, не копейки), `payPurpose`
(назначение), `payer{name,inn,acct,bankName,bicRu}` (для Credit — отправитель; есть ещё
`counterParty`/`receiver`), `category` (incomePeople = переводы физлиц). Парсер —
`normalize_statement_op`.

**Эквайринг-зачисления в выписке** (`is_acquiring_settlement`): плательщик — банк-эквайер
(ИНН `7710140679` АО «ТБанк», или «тбанк»/«тинькофф» в имени). Это сводные/терминальные
зачисления эквайринга, НЕ прямые переводы клиентов → помечаются `is_settlement=True`
(серые, непривязываемые). 🛑 Эти деньги придут ВТОРОЙ раз пер-клиентски через эквайринг-вебхук —
задвоения в учёте нет, т.к. settlement не создаёт Payment.

**Поллинг** (`tasks.py:poll_statement` → `_poll`): beat `accounting-poll-statement` (config/celery.py)
ежечасно; внутренний throttle `ACCOUNTING_POLL_MIN_INTERVAL_HOURS` (деф. 3ч) через последний
`SourcePoll(ok=True)`. SETNX-лок `acct:poll:statement`. Перехлёст `since = last_ok - 2 дня`
(платёж появляется в выписке с задержкой; дедуп по external_id страхует). Гейт
`ACCOUNTING_STATEMENT_POLL_ENABLED`. Ручной запуск — кнопка «Проверить сейчас» (`poll_now`).

## Источник 2 — Эквайринг (webhook + prepay, НЕ поллинг)

Интернет-эквайринг НЕ умеет «список операций за период» — только нотификации (push) и
`GetState` по PaymentId. Поэтому приём = **вебхук**.

**Вебхук** `POST /accounting/acquiring/webhook/` (`views.acquiring_webhook`, csrf_exempt,
публичный): проверка подписи → при `Status=CONFIRMED` создаёт `IncomingPayment(source=acquiring)`
→ обогащает ФИО/телефоном из prepay → отвечает телом `OK`. Тонкий (как у WhatsApp).

🛑 **Подпись (`integrations.acquiring_token`):** SHA-256 от конкатенации значений корневых
скаляров (без `Token` и без вложенных DATA/Receipt) + `Password`, сорт по ключам. **Булево
`Success` сериализуется как `"true"/"false"` (нижний регистр!)**, а не Python `str(True)="True"` —
иначе 403 (это и был первый баг на live). См. `_token_value`.

🛑 **ТБанк НЕ возвращает введённые клиентом ФИО/телефон.** Поле `DATA` из Init обратно НЕ приходит
ни в нотификации (там только `Pan/Amount/OrderId/PaymentId/Status/CardId/ExpDate/Success/ErrorCode/TerminalKey`),
ни в `GetState` (Status/OrderId/Amount/Params). Единственная управляемая ниточка обратно — `OrderId`.

**Решение — prepay-эндпоинт** `POST /accounting/acquiring/prepay/` (`views.acquiring_prepay`,
csrf_exempt, публичный): страница оплаты `fo-y.ru` перед `pay()` шлёт `{order_id, name, phone, amount}`
(`navigator.sendBeacon`, form-encoded), мы пишем `AcquiringPrepay`. Вебхук склеивает по `OrderId`
(`views._enrich_from_prepay`) — подставляет ФИО/телефон в платёж.

**Страница оплаты `fo-y.ru`** — штатный виджет ТБанка `tinkoff_v2.js`, форма с полями
`amount/description/name/phone`. Боевой терминал `1643988098974`. Что добавлено: скрытое поле
`order`, `onsubmit` генерит уникальный OrderId, кладёт в `order`, шлёт `sendBeacon` на наш prepay,
затем зовёт `pay()`. NotificationURL виджет НЕ передаёт — ставится на уровне ТЕРМИНАЛА в ЛК
(Магазины → Терминал → Уведомления → HTTP) = `https://siricrm.ru/accounting/acquiring/webhook/`.
🛑 У терминала ОДИН NotificationURL; на `fo-y.ru` своего учёта нет (только редирект на SuccessURL),
поэтому NotificationURL свободен — заняли без риска для сайта. SuccessURL (страница «спасибо») —
другое, не трогаем.

## Env-переменные (`config/settings/base.py`, секреты в `.env.*`)

```
TBANK_BUSINESS_API_TOKEN   # токен T-API: ЛК Т-Бизнес → Интеграции → Выпуск токена
TBANK_ACCOUNT_NUMBER       #   (доступы «Информация о счетах» + «...об операциях») + № р/с
TBANK_ACQUIRING_TERMINAL_KEY  # TerminalKey интернет-эквайринга
TBANK_ACQUIRING_PASSWORD      # пароль терминала (для проверки подписи)
ACCOUNTING_STATEMENT_POLL_ENABLED=true   # гейт поллинга выписки
ACCOUNTING_ACQUIRING_POLL_ENABLED=true
# деф: TBANK_BUSINESS_API_BASE=https://business.tbank.ru/openapi
#      TBANK_ACQUIRING_API_BASE=https://securepay.tinkoff.ru/v2
#      ACCOUNTING_POLL_MIN_INTERVAL_HOURS=3
```
Пусто → источник «не настроен», поллинг no-op. Шаблоны — `.env.{dev,prod}.example`.

## Эндпоинты

```
/accounting/                       панель (use_htmx)
/accounting/tab/{bank,notifications,payments}/   вкладки (HTMX-партиалы)
/accounting/payment/detail/?ip=    платёжка (read-only)
/accounting/bind/{modal,client-search,charges,execute,unidentified}/   привязка
/accounting/poll-now/              ручной опрос выписки
/accounting/acquiring/prepay/      ПУБЛИЧНЫЙ: данные со страницы оплаты (sendBeacon)
/accounting/acquiring/webhook/     ПУБЛИЧНЫЙ: нотификации эквайринга ТБанк
```

## 🛑 Гоччи (свод)

1. **Подпись эквайринга:** булево → `"true"/"false"` нижним регистром (`_token_value`).
2. **ТБанк не отдаёт ФИО/телефон** ни в нотификации, ни в GetState — только через prepay (OrderId).
3. **Эквайринг в выписке = сводные суммы** (плательщик «АО ТБанк», ИНН 7710140679) → `is_settlement`,
   непривязываемы; пер-клиентика — из эквайринг-вебхука.
4. **`accountAmount` выписки — в рублях**, а `Amount` эквайринга — в копейках (/100).
5. **Деплой при смене env:** deploy-хендлер делает `restart` и НЕ перечитывает env_file → для новых
   `TBANK_*` нужен `up -d --force-recreate web celery celery-beat` (через `ssh siri-prod`).
6. IP для белого списка ТБанк (если попросят): prod `45.90.35.187`, dev `5.35.94.218` (egress = публичный
   IP хоста; ТБанк не в split-tunnel).
7. Модалка: закрывать только на POST-сабмите, иначе hx-get поиска её схлопывает.

## Тест/отладка

```python
# Init тестового платежа (тест-терминал 1643988098974DEMO):
p = {"TerminalKey": TK, "Amount": 10000, "OrderId": oid, "NotificationURL": ".../webhook/"}
p["Token"] = integrations.acquiring_token(p, PWD)
requests.post(f"{base}/Init", json=p)   # → PaymentURL; тест-карта 4300000000000777, 12/30, CVC 123
# GetState: {"TerminalKey","PaymentId","Token"} POST {base}/GetState
# Симуляция нотификации: собрать dict, Token = acquiring_token(dict, PWD), POST на webhook
# management: python manage.py accounting_demo [--clear]   # демо-входящие для UI
```

## Деплой / факты

- Боевая интеграция (выписка + эквайринг) ЖИВАЯ на проде с 14.06.2026 (коммит `ad2570a`),
  эквайринг проверен реальным платежом (prepay из fo-y.ru + нотификация ТБанк IP 91.218.132.2).
- Поллинг выписки на проде автономен (beat вкл.). Эквайринг — вебхук на боевом терминале.

## TODO / дальше по ТЗ (не сделано)

1. **Уведомления сотрудников о поступлении и разнесении платежей + событийка.** При новом входящем
   платеже → уведомление бухгалтеру (через `apps/notifications`); при привязке → `record_action`
   в логе клиента (СОБЫТИЙКА, `apps/crm/client_log.py`) + уведомление ответственному/исполнителю.
2. **UI/UX оплаты с сайта — гарантировать привязку к клиенту.** Сейчас ФИО/телефон вводит клиент
   (возможны опечатки) → разнесение вручную. Доработать: авто-матч клиента по телефону
   (`find_client_by_phone`) на prepay/вебхуке, подсветка/предзаполнение клиента в модалке привязки;
   в идеале — генерить платёжную ссылку из CRM с зашитым `OrderId=client/service` (тогда привязка
   автоматическая, без свободного ввода).
3. **Оплата через мессенджеры.** Кнопка в чат-панели → `Init` с `OrderId=client/service` → ссылка
   в TG/WA/MAX (через существующие sender'ы) → нотификация `CONFIRMED` → авто-привязка по OrderId.
4. **UI бухгалтера — доработать.** Специфичный поиск (по сумме/ФИО/назначению/контрагенту), доп.
   фильтры (период, сумма, источник), **архив** обработанных, отдельная работа с **неопознанными**
   платежами. Сейчас поиск-заглушка, фильтры — статус/тип/источник.

Прочее: механика вкладки «Банк» под загрузку файловых выписок (если понадобится ручной импорт).
