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
