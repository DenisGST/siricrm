# Лиды / Телефоны / Маршрутизация

Подробности рефакторинга телефонов и маршрутизации лидов. Кратко в `CLAUDE.md`.

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

## Заявки из Telegram-канала лидов (формат сообщений)

**Прод, с 27.08.2026.** Канал `-1004426367072` «Банкротство физических лиц с юридическим сопровождением», бот **@SiriusCRMBot** в нём администратор, `TELEGRAM_LEADS_CHANNEL_ID` в `.env.prod`. Читаем поллингом (`poll-telegram-leads`, см. CLAUDE.md — webhook у нас не работает).

🛑 **Канал, а не группа.** Заявки постит бот сайта, а Telegram **не отдаёт ботам сообщения других ботов в группах** — ни админам, ни с выключенным privacy mode. В канале ограничения нет: посты приходят как `channel_post` независимо от автора. Если поток однажды переедет в группу — читать её придётся userbot'ом на Telethon (обычный аккаунт видит всё).

**Форматов два, `_parse_lead` разводит их по полям** (`apps/telegram/leads_bot.py`):

| | Канальный (текущий) | FlexBe (исторический) |
|---|---|---|
| Признак | есть «Сайт:» / «Лендинг:» | «Новая заявка №N … со страницы X» |
| Номер заявки | поле `ID` | `№N` в заголовке |
| Категория | из заголовка «Новая заявка · Стоимость» | «Название формы:» |
| Ответы | все пары «Ключ: значение» | блок «Данные формы:» |

Канальный формат отдают три сайта — `sirius-bfl.ru`, `про-долги.рф`, `небанкрот.рф`. 🛑 Два последних приходят в **punycode** (`xn----ftbcrnpcej.xn--p1ai`), разворачиваем в кириллицу — иначе менеджер видит в источнике лида абракадабру.

🛑 **Состав полей плавает и будет меняться.** У формы обратного звонка есть только телефон; у квиза добавляются «Имя» и ответы (регион, сумма долга, кредиторов, исполнительные производства, имущество, ипотека, источники дохода). Поэтому разбираем **все** пары «Ключ: значение», а не фиксированный список: новый вопрос доедет в карточку лида сам. В отдельные поля вытаскиваются `Имя` → ФИО, `Телефон`, `ID`, `Сайт`, категория; `Устройство` (UA) и `IP` отбрасываются как шум. Лид без распознанного телефона не теряется: исходное сообщение целиком уходит в примечание.

**Что происходит с заявкой:** дедуп по телефону (`find_client_by_phone`) → новый клиент + `route_new_lead` (услуга БФЛ, сотрудники с галкой `accept_telegram_leads`) **либо**, если клиент уже есть, только событие «Повторный лид» с данными заявки. В обоих случаях клиент ставится на доску колл-центра — `apps.callcenter.intake.handle_telegram_lead`, колонка с флагом `catch_telegram_leads` (на проде «Новые обращения»).

**Диагностика:** `manage.py telegram_leads_probe` — показывает, что бот видит в канале, его `chat_id` и результат разбора каждого сообщения, ничего не создавая в CRM. 🛑 Берёт тот же SETNX-лок, что и поллер; при работающем beat возможен 409 Conflict.
