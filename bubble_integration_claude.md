# Bubble-импорт (`apps/bubble_import/`)

Перенос данных из старой CRM на bubble.io в SiriCRM. Через Bubble Data API + cursor-пагинация. Промежуточный буфер — модель `BubbleRecord` (JSONB raw + дискриминатор `entity` + `status`), потом appliers переводят в Client/Service/Message/StoredFile/Payment/etc.

## Особенности

- **Лимит Bubble Data API на cursor-пагинацию — 50 000** записей. Сущности с большим объёмом (`MessageWSP`, `Files`) дофетчиваются **окнами по 30 дней** через `services.fetch_window()`. См. `tasks.WINDOWED_ENTITIES` + `WINDOW_YEARS_BY_ENTITY`. Идемпотентно через `update_or_create(entity, bubble_id)` — повторный fetch не дублирует.
- **`ProjectBFL.telWSP`** = WhatsApp-номер клиента по конкретной услуге. `apply_projectbfl` пишет его в `ClientPhone(purpose='whatsapp')` алиас — тот же клиент может писать с нескольких номеров.
- **WA-медиа vs Files**: `StoredFile.bubble_id` имеет префикс `wamedia_<msg_id>` для медиа из чатов и чистый bubble id для документов из таблицы `Files` — пересечений в БД нет (могут быть в S3 как двойные ключи, безвредно).
- **Долгие job'ы**: `full_import_task` имеет `time_limit=24h`. Для длинных apply'ев (часы) запускай через `docker compose exec -d web sh -c "nohup python manage.py apply_bubble <Entity> > /tmp/x.log 2>&1 &"` — переживёт SSH-разрыв.

## Management-команды (для дочистки после массового импорта)

- `sync_projectbfl_aliases` — пройтись по уже импортированным ProjectBFL и заполнить `ClientPhone(whatsapp)` из `raw.telWSP`.
- `reapply_failed_wa` — сбрасывает MessageWSP с ошибкой «клиент не найден» в `pending+approved` и прогоняет; полезно после sync алиасов.
- `create_leads_from_failed_wa` — для оставшихся непривязанных номеров создаёт клиентов-лидов (как при онлайн-обращении) и сбрасывает их сообщения в pending для повторного apply.
- `fetch_bubble_since N [--apply] [--by created|modified]` — доливка изменений из Bubble за последние N дней. Идемпотентно через `update_or_create(entity, bubble_id)`. По умолчанию `--by created` — берёт только новые записи; `--by modified` — также обновлённые.

## Прод vs тест Bubble

Bubble имеет **две версии** одного приложения: live (`/api/1.1/obj/...`) и development (`/version-test/api/1.1/obj/...`). Боевые данные клиентов — **только в live**. Текущий env: `BUBBLE_API_BASE=https://siricrmdev.ru/api/1.1/obj`. Если ошибочно поставить `/version-test/` — fetch будет возвращать 0 новых, а в кабинете Bubble будут видны другие записи (это разные базы).

> ⚠ **`BubbleRecord.target_id` — это `str`, не `UUID`.** При маппинге на нашу модель (`Service.id`, `Client.id` — UUID) нельзя сравнивать в set'е напрямую: `target_id in service_ids` будет False даже при совпадающих значениях. Конвертировать: `uuid.UUID(str(br.target_id))`. Django ORM сам конвертирует в `filter(pk__in=[...])` (поэтому update'ы из такого set работают), но set-difference / dict-key lookup — тихо ломается. Прецедент — `backfill_status_from_bubble` (первый прогон проставил Service правильно, но Client.status весь оказался `unknown` из-за этого).

## Backfill StatusPrj (этапы услуг + статусы клиентов)

`python manage.py backfill_status_from_bubble [--dry-run]` (`apps/bubble_import/management/commands/`) — тянет `StatusPrj`-таблицу из Bubble (16 записей с `nameStatusPrj`), по жёстко прошитому `STATUS_MAP` в файле команды ставит `Service.common_status` всем 5764 услугам и пересчитывает `Client.status` по приоритету `PRIORITY = {active:1, closed:2, lead:3, unknown:4, refused:5, archive:6, to_delete:7}` (для каждого клиента — наивысший статус среди его услуг). Идемпотентна. Услуги без `statusPrj` / backing ProjectBFL → дефолт «Лидогенератор» (вклад в клиента — `unknown`). Клиенты без услуг — не трогаются.
