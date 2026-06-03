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

## Files-сущность: особенности

- **«Свежие Files» с пустым `linkGDrive` — это placeholder-записи Bubble на каждое WA-сообщение** (поле `comments: "Автосохранен из Whatsapp"`, `filename: "wamid.xxxx"`). В самом Bubble файла нет — реальный WA-медиафайл уже у нас в S3 через webhook (`StoredFile.bubble_id='wamedia_<wamid>'`). `apply_files` корректно скипает их (пустая ссылка → `status='skipped'`).
- **Bubble хранил «настоящие» документы в Google Drive** через `linkGDrive`. На сегодня (31 мая 2026) большинство таких ссылок мертвы (404/410/HTML stub), а живые требуют `_gdrive_fetch_with_confirm` (handshake-запросы, до минуты на запрос). При полном apply Files это блокирует всю очередь.
- **Стратегия apply Files**:
  1. `BubbleRecord.objects.filter(entity='Files', status='pending', approved=True).filter(raw__linkGDrive__icontains='google.com').update(approved=False)` — снять с очереди GDrive.
  2. Запустить `apply_bubble Files` — пройдёт остальное за минуты (в основном skip + единицы HTTP-запросов на не-GDrive хосты).
  3. **GDrive-хвост — отдельным проходом** (если когда-нибудь понадобится): вернуть `approved=True` для них, снизить timeout до 5с в `download_to_storedfile`, прогнать в фоне на ночь. Реально живых там единицы.
- На 31 мая 2026 после доливки `fetch_bubble_since 7 --apply`:
  - Imported Files (исторических): **92 234**.
  - GDrive отложены (`approved=False`): **837**.
  - WA-placeholder skipped: ~411 000 (нормально, без файла в Bubble).
  - Реально новых документов за неделю: 0–1 (отдел перешёл на SiriCRM, в Bubble больше не работают).

## Доливка из Bubble на prod — ловушка с `--entities`

`fetch_bubble_since N --apply` **по умолчанию** идёт по 11 сущностям (`DEFAULT_ENTITIES`: User, Man, ProjectBFL, Organization, Kreditors, PropetyAnketa, Events, Сorrespondence, Money, MessageWSP, Files). Запускать **только** через явный список:

```bash
python manage.py fetch_bubble_since 120 --entities Files --apply
```

Прецедент 31 мая 2026 ночью: запустил без `--entities` чтобы дотянуть свежие Files Авад → начал обновлять весь raw 9 сущностей и помечать `approved=True` тысячам не-imported записей. Прервали рестартом web. `approved=True` остались висеть (откатить точно невозможно — у `BubbleRecord` нет `updated_at`, отличить «сегодня» от «вчера» нельзя). Безопасно пока не запускается `apply_approved` массово. Урок: всегда `--entities`.

## Prod-кэш Files обрезается датой последнего fetch'а

На prod max `BubbleRecord(Files).bubble_created` = **2026-02-09** (свежее не пофетчили). Если в Bubble есть новый ProjectBFL — его файлов в нашей `BubbleRecord` нет, `apply_files` к ним не доберётся. Симптом — клиент в Siri без файлов, хотя в Bubble UI они открываются (прецедент: Авад Елена, ProjectBFL `1776929144653x271822434412527600`, в Bubble 95 файлов, у нас 0).

**Точечный backfill «один клиент»** — обходным путём через Bubble API напрямую (без `BubbleRecord`-буфера):

```python
# В manage.py shell
from apps.bubble_import import bubble_api
from apps.bubble_import.appliers import download_to_storedfile, _bubble_folder_path
from apps.bubble_import.extractors import clean_str
from apps.files.models import ClientFile

constraints = [{"key": "projectBFL", "constraint_type": "equals", "value": SVC_BID}]
cursor = 0
all_files = []
while True:
    page = bubble_api.fetch_page("Files", cursor=cursor, limit=bubble_api.PAGE_LIMIT, constraints=constraints)
    results = page.get("results", [])
    if not results: break
    all_files.extend(results); cursor += len(results)
    if page.get("remaining", 0) <= 0: break

for f in all_files:
    link = clean_str(f.get("linkGDrive"))
    if not link: continue
    stored = download_to_storedfile(link, clean_str(f.get("filename")) or f"file_{f['_id']}", f["_id"])
    folder = _bubble_folder_path(client, clean_str(f.get("directory")))
    ClientFile.objects.get_or_create(stored_file=stored, folder=folder,
        defaults={"name": (clean_str(f.get("filename")) or "")[:255], "size": stored.size or 0, "content_type": stored.content_type})
```

Авад 31 мая: 95 файлов → 80 OK / 15 skip (пустой linkGDrive) / 0 err. Свежие (после 9 февраля) GDrive-ссылки **массово живы**, в отличие от старых из основной волны.

## WA-медиа: `MessageWSP.url_file` vs `body`

`apply_messagewsp` (`appliers.py:677`) при медиа-сообщении пытается скачать файл **только из поля `body`** (если `body.startswith("http")`). А **`url_file`** — отдельное поле, которое появилось позже как канонический URL медиа в Bubble — **игнорируется**.

При импорте 31 мая 2026 нашли:
- 43 215 MessageWSP с непустым `url_file` (99% → drive.google.com).
- 40 787 из них уже имели Message.file (старые записи, когда apply брал URL из `body`).
- 6 526 MessageWSP с url_file имеют Message **без файла** — кандидаты на доливку.

**Точечный backfill по конкретному автору** — через Bubble API constraint `author = "<phone>@c.us"`, забрать все MessageWSP, отфильтровать с непустым `url_file`, раскладывать в файловый менеджер клиента **в `Чат/Полученные` (`fromMe=False`) или `Чат/Отправленные` (`fromMe=True`)** — терминология «полученные/отправленные» принята пользователем, **не** «входящие/исходящие». Пример (Авад, 31 мая): 73 MessageWSP по `79377051717@c.us` → 43 с url_file → 43 OK / 0 err.

**Backfill-скрипт (31 мая, общий по всем без файлов)** (`/tmp/wa_media_backfill2.py` на prod):
```python
candidates = Message.objects.filter(
    channel="whatsapp", file__isnull=True, bubble_id__isnull=False,
).exclude(bubble_id="").exclude(message_type="text").values_list("id","bubble_id")
# Для каждого: BubbleRecord(MessageWSP, bubble_id) → raw.url_file → download_to_storedfile
```

Идти **от Message** (≈6.5k), а не от MessageWSP (≈260k) — иначе `raw__url_file__icontains` тормозит JSONB. Итог прогона: **2 124 ok / 4 402 err** из 6 526 (~33%, ~0.3 GB). Не однородно: первые ~4 000 — массово мёртвые (старые ссылки Google Drive), оставшиеся ~2 500 — почти 100% живых.

## Привязка исполнителей по услугам / ответственных по клиентам из Bubble

`_assign_service_employees` (`appliers.py:467`) при импорте читает поля `Manager / ROP / Jurist / Arbitragnik` и складывает их в `Service.employees` + `ServiceEmployeeState`. Но в реальных ProjectBFL также есть:
- **`ownerDoc`** — сборщик документов (Оксана Тароватова и ещё 3 сотрудника отдела). НЕ резолвится по умолчанию — поэтому большинство клиентов на этапе «Сбор документов» висели без исполнителя.
- **`executorDogovor`** — поле есть, но **не ссылается на Bubble User** (другая таблица). Резолвится в 0 случаях.

**Что делать по новой схеме «ROP-Власов особый»** (применено 31 мая 2026):
- ROP = Власов → `Client.employees.add(Власов)` + `Service.employees.add(Власов)`.
- ROP = другой → `Client.employees.add(тот_другой)`; в `Service.employees` НИКОГО не добавляем.
- Manager / Jurist / Arbitragnik / **ownerDoc** → всегда в `Service.employees`.
- `Service.employees.add(...)` идемпотентен (M2M), плюс пишем дублирующе в `ServiceEmployeeState.objects.get_or_create()`.

Скрипт-разовка (не management-команда; запускали через `python manage.py shell`). Итог: **+4 992 связки Client.employees, +984 связки Service.employees** (Власов в Service уже массово был, основной прирост — Manager/ownerDoc/Arbitragnik).

> **Bubble UID Власова** — `1627026133165x761202125577024500` (через `BubbleRecord(User, target_id=Employee.id)`). Используется как маркер «ROP=Власов».

## Дедубликация клиентов после импорта

Импорт мог создать дубли клиентов (тот же человек попал из Bubble Man + из Telegram-чата + из WA-канала). Чистили вручную через `python manage.py shell` (UI у `client_merge` есть, но он переносит только messages/services/employees — недостаточно).

**Полный мерж** должен переносить ВСЁ что FK→Client:
- `Message`, `ClientLogEntry`, `Service`, `ClientFolder` (для файлов через `ClientFile.folder.client`)
- `ClientEmployee` (M2M через through), `ClientPhone`
- **`Payment.client`, `Charge.client`** — ⚠ у этих моделей **прямой FK на Client** (плюс через `Service`). Если переносить только через Service, удаление source клиента каскадно снесёт его Payment/Charge.
- При конфликте unique-полей source (`telegram_id`, `username`): сначала очистить их у source (`source.username=""; source.telegram_id=None`), потом проставить target.

> ⚠ **Прецедент 31 мая 2026**: мерж Смирнова Ивана Александровича пошёл без переноса `Charge.client` → каскад снёс **16 начислений**. Восстанавливали из backup `db-20260531-142120.sql.gz`: `gunzip | awk` секцию `COPY public.finance_charge` → grep по UUID source → `sed` подменить client_id/service_id на target/canonical → `psql COPY finance_charge FROM stdin`.

**Логика отбора пар:**
- **Совпадение телефона** (нормализованный E.164) — 100% дубль, всё равно показать пользователю (часто это разные члены семьи на одном номере — супруги, родители).
- **100% совпадение нормализованного ФИО + любой второй сигнал** (`birth_date / inn / passport_number / email`) — высокая уверенность, тысячи однофамильцев исключаются.
- 95%+ ФИО без второго сигнала — слишком много шума (Ивановы, Кузнецовы), без подтверждения смысла мало.

**Шаблоны pair-types:**
- «пустая lead + полная active» — основной массив, бот-пара после импорта (лид-карточка + реальная). Автомерж в активную.
- «обе пустые» — дубли placeholder без данных, любую удалить.
- «обе полные» — спрашиваем пользователя по полям (могут быть разные паспорта = разные люди).

**Состояние на 31 мая 2026:** prod 6406 → ~5500 клиентов. По телефону объединено 9, по ФИО+сигнал — 166, пропущено как «разные люди» (Игнатенко с разными паспортами, Юрченко с супругой, и т.д.) — единичные кейсы. Snapshot каждой удаляемой записи лежал в `/tmp/merge_snapshot_<src_uuid>.json` на prod web (до рестарта).
