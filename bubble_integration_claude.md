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

## Массовый backfill Files+WA-медиа (1–2 июня 2026)

Пробежался по всем клиентам в статусах `unknown/lead/active` (1319 шт) — дотягивал Files (по `projectBFL` constraint) и WA-медиа (по MessageWSP constraint). Скрипт `/tmp/bulk_backfill2.py` внутри `siricrm-web-1` с checkpoint в `/tmp/bulk_checkpoint` (id последнего обработанного клиента). Идемпотентный — повторный прогон с тем же checkpoint'ом skip'ает уже скачанные через `StoredFile.bubble_id`. Итог: ~6450 файлов + ~1100 WA-медиа за оба захода.

### Гочча с фильтром `author` vs `chatId` (исходящие WA теряются)

Первая версия скрипта фильтровала MessageWSP по `author=<phone>@c.us` — это ловит **только входящие** сообщения от клиента. **Исходящие** (наши ответы клиенту: договоры, доверенности, согласия в PDF) имеют `author=наш_номер@c.us` и `fromMe=True`, но `chatId=<phone>@c.us` (где phone = собеседник). Конкретно на тесте: у Бурмистенко Кристины `author` дал 14 записей, `chatId` дал 28 — половина PDF потерялась.

**Правильный фильтр: `chatId`**, не author. И это касается не только backfill: любой код, который собирает «всю переписку с клиентом» через MessageWSP, должен идти по `chatId`.

### `body` в WA-медиа сообщениях — это CDN URL, не имя файла

`MessageWSP.body` для media-сообщений (PDF/JPG) **= URL на Bubble CDN** (`https://46c211e3e2eb1e999ed29a66a900786a.cdn.bubble.io/f<id>/<filename>`), а не текст сообщения. Если делать `fname = body[:40]` — получится мусор вроде `https:__46c211...`. Правильно — `basename` от `url_file` или `body` через `urllib.parse.unquote(urlparse(url).path).rsplit("/", 1)[-1]`. И для `apply_messagewsp`, и для самописных backfill'ов это надо учитывать. Прецедент: после bulk_backfill2 пришлось делать отдельный rename-проход — у клиента 4 файла из 7 имели уродские имена.

### Двойные StoredFile (`wamedia_<bid>` vs `<bid>`) и дедуп

Для одного и того же медиафайла в системе могут оказаться **два StoredFile**:
- bulk_backfill2 (и старые скрипты) кладут с `bubble_id = "wamedia_" + msg_bid` — это **суррогатный** id, не существующий в Bubble Data API.
- `apply_messagewsp` (полноценный applier) кладёт с `bubble_id = msg_bid` (чистый Bubble `_id`).

Если применить applier на MessageWSP уже подкаченные через bulk_backfill — образуется пара дубликатов на один S3-blob. `Message.file` обычно ссылается на старый (`wamedia_*`). Дедуп: перекинуть `Message.file → new_sf` (с правильным bubble_id), удалить второй ClientFile и StoredFile, S3-байты оставить мусором.

### Bubble CDN 403 / refetch `GET /MessageWSP/<id>`

CDN-ссылки Bubble (`*.cdn.bubble.io/f<id>/...`) — **подписанные**, через время отдают `403 Forbidden`. У Бурмистенко 3 PDF при первой попытке упали с 403. Решение — обратиться к Bubble Data API за одной записью: `GET https://siricrmdev.ru/api/1.1/obj/MessageWSP/<bubble_id>` — Bubble отдаёт свежий объект, где `url_file` уже **GDrive direct link**, который скачивается через стандартный `_gdrive_fetch_with_confirm`. То есть CDN — кэш, а Data API ходит к источнику.

### Полный точечный реимпорт клиента из Bubble

Для конкретного клиента (Бурмистенко) восстановили **всё что есть в Bubble**, в т.ч. не-Files данные:

```python
# 1. Найти все BubbleRecord, упоминающие клиента (Man bid или ProjectBFL bid)
from django.db import connection
with connection.cursor() as cur:
    cur.execute("""
      SELECT id FROM bubble_import_bubblerecord
      WHERE raw::text LIKE %s OR raw::text LIKE %s OR bubble_id IN (%s, %s)
    """, [f"%{MAN_BID}%", f"%{PBFL_BID}%", MAN_BID, PBFL_BID])

# 2. apply_record() на каждую — applier'ы идемпотентны (lookup по bubble_id, update_or_create)
from apps.bubble_import.appliers import apply_record
for br in qs: apply_record(br)
```

Покрытие entity у одного клиента: Man (1), ProjectBFL (1), Events (1), Kreditors (5), Money (17), PropetyAnketa (1). После apply подтянулись: паспорт, дата рождения, адрес, длинные `notes`, суммы договора, 5 кредиторов, 16 начислений (`Charge` через Money applier, существующие обновились), 1 имущество. Что НЕ подтянулось из дефолтного applier'а — `inn/snils/email/addresses` (в Bubble Man-поля для них есть `AdresR`/`postIndex`/`numbHouse`, но applier не создаёт `ClientAddress` из них; это задача отдельного PR).

### bulk_backfill2.py — ключевые уроки

- **siricrm-web-1 регулярно рестартается** (4 раза за импорт — на deploy, ручные рестарты пользователя, иногда сам). Скрипт переживает рестарт через checkpoint, но каждый раз надо вручную найти PID убитого процесса и запустить заново. Для долгих backfill'ов **лучше переехать в `siricrm-devops-runner-1`** — он не рестартается на deploy.
- **`kill` отсутствует** в alpine-подобном контейнере (`exec: kill: not found`). Для остановки процесса берёшь хостовой PID из `docker top siricrm-web-1` (это и есть PID на хосте) и делаешь `kill -TERM <hostpid>` **с хоста**. Внутри контейнера через `docker exec ... kill` не сработает.
- **DNS внутри контейнера может «временно» отвалиться** на 5–10 минут (`Temporary failure in name resolution` на `siricrmdev.ru`). На длинном прогоне это даст десятки клиентов с пустым результатом (Files API err + WA API err в логе). После восстановления DNS — отдельный retry-скрипт по этим ID:
  ```bash
  # Вытащить ID пострадавших из лога
  grep -oE '\[[a-f0-9-]+\] (Files|WA) API err' .log | grep -oE '[a-f0-9-]{36}' | sort -u > retry_ids.txt
  ```
  Один реальный кейс: 97 клиентов из 359 в сессии получили DNS-err.
- **Двойные кавычки в `sed`** через `docker exec ... sh -c "sed ..."` ломаются из-за многослойного экранирования. Правильнее: `docker cp script.py /tmp/`, патчить локально через `Edit`, `docker cp` обратно.

### `BUBBLE_API_BASE` после правок env

Если меняли `BUBBLE_API_BASE` в `.env*` — для скриптов внутри `siricrm-web-1` нужно либо `docker compose up -d --force-recreate web` (читает env), либо явный `os.environ["BUBBLE_API_BASE"]="..."` в скрипте. Просто `docker compose restart web` НЕ перечитывает env (общая ловушка из основного CLAUDE.md).

## Регулярный дозалив из Bubble (4 июня 2026)

Поскольку юристы и часть отделов ещё работают в Bubble, нужны еженедельные «дельта»-дозаливы. Безопасная последовательность:

### 1. Диагностика — что отстало

```python
from django.db.models import Max, Count
from apps.bubble_import.models import BubbleRecord
# Текущий max bubble_created по entity vs API count_total → разница = что нужно дотянуть
```

Сразу за всю историю — сравнить `BubbleRecord.objects.filter(entity=X).aggregate(Max("bubble_created"))` против `bubble_api.count_total(X)`.

### 2. Fetch без `--apply`

```bash
python manage.py fetch_bubble_since 7 --entities "Man,ProjectBFL,Events,Money,Сorrespondence,Kreditors,PropetyAnketa"
```

> ⚠ `--entities` принимает **comma-separated string**, не nargs. `--entities Man ProjectBFL` упадёт. Кириллическая `С` в `Сorrespondence` — не латинская.

После fetch смотрим что пришло: счётчики «new/updated», и количество approved-pending в БД. Без `--apply` ничего не льётся в Client/Service.

### 3. По одному в интерактиве (для Man)

При дозаливе новых клиентов: каждый новый Man **проверяем сначала по phone в Siri** (через `find_client_by_phone`/`Client.phones`). Возможные сценарии:

- **0 совпадений** → чистый apply (новый клиент)
- **1 совпадение** → `apply_record(br)` c `overrides.merge_into_client_id=<siri_id>` — applier подтянет паспорт/notes/gender в существующего, прицепит bubble_id
- **2+ совпадений по phone в Siri** (типичная ситуация WhatsApp-автолид + менеджерская карточка) → сначала мерж двух Siri-клиентов, потом apply с `merge_into_client_id` в canonical
- **Совпадение по ФИО без phone** → СПРОСИТЬ оператора (может быть однофамилец/тёзка). Прецедент: «Бондаренко из Волгограда» при существовании другой Бондаренко в Siri — applier по ФИО НЕ мерджит, создаст отдельного.

### 4. Овайды через `BubbleRecord.overrides` (JSON)

`apply_man` читает три ключа из `rec.value(...)` (overrides приоритетнее `raw`):
- `phone_override` — переопределить телефон (для агентских клиентов без реального номера, см. ниже)
- `merge_into_client_id` — явно указать существующего Siri-клиента для слияния (используется когда `bubble_id` ещё не привязан, а по phone совпадение неоднозначное)
- `force_rename_existing` (bool) — при `merged_existing=True` перезаписать ФИО target данными из Bubble (обычно `False` если у target уже полное ФИО)
- `overwrite_dup` (bool) — отметить запись для перезаписи существующего дубля по телефону

### 5. Агентские клиенты (без реального телефона)

Часть клиентов в Bubble — **агентские**: пришли через посредников (Колчаев, Касимовский, Григоренко и др.), связи нет, в `tel` стоит `0000000000` (тех. заглушка). Applier их по умолчанию skip'нет («Телефон-заглушка»). Они **не мусор** — у них есть активные начисления (Money), события (Events), кредиторы (Kreditors). Юристы ведут их через агентов.

**Схема дозалива:**
1. Восстановить approved=True у Man+ProjectBFL+children (status='pending')
2. Каждому Man выдать `overrides.phone_override` = «+79000000XX» (уникальный фиктивный, последние 2 цифры — счётчик)
3. apply Man → ProjectBFL → Money/Events/Kreditors
4. После создания Client — проставить `Client.referral_source = "Агент <Фамилия>"`. Имя агента берётся из notes Bubble (часто «агент Колчаев» в `notes`/`namePrj` raw) либо у оператора

> ⚠ В поиске «4 июня 2026» это **21 клиент** (Кузнецов Н., Юссеф, Осипов, Кудашов, Михеев, Кузнецов Д., Товстая, Суханов, Курдяева, Антюфеева, Коннова, Артемов, Петров, Желтухина, Бондаренко из Волгограда, Гущин, Шамшина, Трифонова, Коротич, Володкин, Кистенева). У последних двух агент не известен.

### 6. Массовый bulk-skip для Files

Files-сущность раздувается WA-placeholder'ами (filename `wamid.*` / comments «Автосохранен из Whatsapp») и пустыми `linkGDrive`. На прод-кэше 424k таких при ~8.6k реальных GDrive-документов. **Пройтись applier-ом по ним — часы** (хоть и быстро на каждой записи). Эффективнее одним SQL:

```python
qs = BubbleRecord.objects.filter(entity="Files", approved=True).exclude(status="imported")
ids = [br.id for br in qs.only("id","raw").iterator(chunk_size=5000)
       if not (br.raw or {}).get("linkGDrive")
       or (br.raw or {}).get("filename","").startswith("wamid")
       or "Автосохранен из Whatsapp" in (br.raw or {}).get("comments","")]
BubbleRecord.objects.filter(id__in=ids).update(status="skipped", approved=False, error="WA-placeholder")
```

Это секунды. Затем `apply_record` остаётся для реальных GDrive (~10k) — их тащить в `siricrm-devops-runner-1` (стабильнее web), занимает ~10–30 минут со скачиванием. Прецедент 4 июня 2026: 8035 imported + 614 dead-link errors на 8649 свежих GDrive.

### 7. Children (Events/Money/Kreditors/PropetyAnketa) и зависимость от ProjectBFL

Их applier'ы падают с `Услуга (ProjectBfl) не импортирована` если родительский ProjectBFL не в Siri. Это нормально: иди по схеме **Man → ProjectBFL → children**, и иногда нужен повторный прогон children после доимпорта родителей. В моих кейсах retry даёт +30–70% (когда добавил недостающие ProjectBFL).

Children error'ы которые остаются после retry — это либо записи без `Project` поля (мусор Bubble), либо ссылки на ProjectBFL которых **никогда не было** в Siri (отказы). Чистить очередь:

```python
BubbleRecord.objects.filter(entity="Money", status="error", approved=True).update(approved=False)
```

### 8. ProjectBFL ↔ существующая Service в Siri = дубль

При apply ProjectBFL в клиента, у которого менеджер уже создал Service (без bubble_id) — у клиента **становится 2 услуги БФЛ**. Сценарий — менеджер занёс лида вручную (Service со статусом), потом мы дотянули из Bubble (Service с bubble_id, но без статуса). Семантически они тождественны. **Автомердж**:

1. Запомнить `services_before = set(client.services.values_list("id", flat=True))`
2. `apply_record(br)` — создаст новую Service
3. `new_svc = Service.objects.get(id=services_after - services_before)`
4. Если `len(services_before) == 1`:
   - перенести `Service.employees` старой → новой
   - `Charge.objects.filter(service=old).update(service=new)` + Payment
   - `old.delete()`

Какую делать canonical — зависит от того у кого `status` заполнен. Обычно у только что созданной из Bubble — статус из Bubble есть, у менеджерской — нет. Был и обратный кейс (Миргалеева 4 июня) — canonical = менеджерская со статусом, src = ребёнок Bubble без статуса.

### 9. Дополнения после ежедневного дозалива (5 июня 2026)

- **Два Bubble Man на одного клиента** — повторяющийся паттерн (был у Кузиной 4 июня, у Трифоновой Арины 5 июня). Менеджер случайно создаёт в Bubble вторую Man-запись (часто с другим bubble_id, но тем же ФИО+датой рождения). Вчерашний apply мог захватить только одну. На следующий день вторая Man (а вместе с ней её ProjectBFL и Correspondence/Money/Events) висят в pending → ошибки «не импортирован». **Лечение:** apply забытой Man с `merge_into_client_id=<существующий Siri>` + опционально `phone_override` (если phone=0000). Потом apply дубль-ProjectBFL → applier создаст вторую Service у того же Client → её сразу слить (см. п.8) и в обоих BubbleRecord ProjectBFL руками выставить `target_id = <оставшаяся Service.id>`, чтобы будущие fetch'и children находили обе через fallback на BubbleRecord.target_id.

- **Опечатки в ФИО Siri исправлять через `force_rename_existing=True`** — `overrides = {"merge_into_client_id": "<siri_id>", "force_rename_existing": True}`. Пример 5 июня: «Трифонов **Владимслав** Анатольевич» → «Трифонов **Владислав** Анатольевич» (менеджер опечатался при ручном создании, Bubble имеет правильное имя). После apply поле перезаписывается из raw.

- **«0 новых ProjectBFL» нормально** при ненулевых новых Man. Когда новый клиент (Man) попадает к существующей услуге (например, ответ из WA на лида без услуги, потом мерж) — ProjectBFL не создаётся, только связь устанавливается.

- **Files за 2-дневный интервал — 0 WA-placeholders** (5 июня: 689 pending = 689 GDrive). Bulk skip пропускается, можно сразу `apply_record` в `siricrm-devops-runner-1`. Чем дольше интервал между fetch'ами — тем больше WA-placeholders накапливается (за 4 месяца было 420k placeholders на 8.6k реальных GDrive). **Делать fetch ежедневно — нет смысла bulk skip'а.**

- **Cleanup dead-link Files после каждого apply** обязательный — иначе 614 мёртвых GDrive 4 июня попадают в очередь 5 июня и опять пытаются скачаться (вижу 614 error из 689 на 5 июня — ровно то же количество). Сразу после apply: `BubbleRecord.objects.filter(entity="Files", status="error", approved=True).update(approved=False)`.

### Ежедневный плейбук дозалива (короткая шпаргалка)

```bash
# 1. Снимок: что изменилось с последнего fetch
python manage.py shell -c "
from django.db.models import Max
from apps.bubble_import.models import BubbleRecord
from apps.bubble_import import bubble_api
for ent in ['Man','ProjectBFL','Сorrespondence','Events','Money','Files']:
    in_siri = BubbleRecord.objects.filter(entity=ent).count()
    total = bubble_api.count_total(ent)
    last = BubbleRecord.objects.filter(entity=ent).aggregate(m=Max('bubble_created'))['m']
    print(ent, in_siri, total, total-in_siri, last)
"

# 2. Fetch БЕЗ apply
python manage.py fetch_bubble_since 2 --entities "Man,ProjectBFL,Сorrespondence,Events,Money,Kreditors,PropetyAnketa"
python manage.py fetch_bubble_since 2 --entities Files

# 3. Apply Man — ИНТЕРАКТИВНО. По каждому в pending проверять Siri по phone/ФИО:
#    - 0 совпадений → apply_record(br)
#    - 1 совпадение → overrides.merge_into_client_id
#    - 2+ → сначала мерж Siri-дублей, потом apply в canonical
#    - опечатка ФИО → force_rename_existing=True
#    - phone=0000 (агентский) → phone_override="+79000000XX", уточнить агента у оператора → referral_source

# 4. Apply ProjectBFL — после Man. Если у клиента уже есть Service → автомердж (см. п.8).

# 5. Apply Сorrespondence/Events/Money/Kreditors/PropetyAnketa — массово, retry после Man+PBFL.
#    Сразу cleanup error: BubbleRecord.objects.filter(entity=X, status='error', approved=True).update(approved=False)

# 6. Files: bulk_skip (только если интервал > 1 недели), apply GDrive в siricrm-devops-runner-1.
#    После apply: cleanup dead-link Files.

# 7. Все skipped/error финального состояния — approved=False, чтобы очередь была чистой к завтрашнему fetch'у.
```

### 10. Миграция отдела с Bubble на Siri (паттерн)

При переезде очередного отдела (Янины — 10 июня, дальше будут юр.отдел и реализация) — однотипная процедура переноса распределения услуг по Siri-канбану конкретного сотрудника, опираясь на Bubble-статусы. Алгоритм:

1. **Получить от пользователя** маппинг Bubble UUID → Siri `ServiceEmployeeStatus.id` (есть свой набор у каждого этапа `ServiceCommonStatus`). Уточнить:
   - на каком `Service.common_status` будут эти услуги (Янина была «Подготовка иска»)
   - куда падают записи **без** Bubble под-статуса (Inbox `is_inbox=True` или какой-то стартовый)
   - добавлять ли сотрудника в `Service.employees` если его там нет
2. **Найти в Bubble нужные ProjectBFL:**
   ```python
   qs = BubbleRecord.objects.filter(entity="ProjectBFL").extra(
       where=["raw->>'statusPrj' = %s"], params=[BUBBLE_STAGE_UUID]
   )
   # Распределение по подстатусу (statusVvod / statusSbor / ...)
   Counter((br.raw or {}).get("statusVvod") for br in qs)
   ```
3. **Подтвердить распределение у пользователя** — если есть неизвестные UUIDs (не из его маппинга) → они попадают в Inbox; если их много, могут быть забытые в маппинге.
4. **Применить в одной транзакции:**
   ```python
   for br in qs:
       svc = Service.objects.filter(bubble_id=br.bubble_id).first()
       if not svc: continue
       svc.common_status_id = COMMON_STATUS_TARGET; svc.save()
       if not svc.employees.filter(id=EMP_ID).exists():
           svc.employees.add(employee)
       state, _ = ServiceEmployeeState.objects.get_or_create(service=svc, employee=employee)
       state.status_id = MAPPING.get(sub_status) or INBOX_STATUS_ID
       state.save()
   ```

Прецедент 10 июня: Янина Нестеркина, «Отдел сбора документов БФЛ», 105 услуг из Bubble этапа «Ввод», маппинг по `statusVvod` (7 подстатусов) + 27 в Inbox. Полный отчёт — в [[dubbling.md]] раздел «Массовые операции».

### 11. Известные баги applier'ов (не исправлено)

- **`Client.snils` принимает значение `raw.snils`** как есть. В Bubble юристы иногда пишут туда **ИНН** в формате «ИНН 272401038090». Сейчас оно ложится в snils обрезанным до 14 char («ИНН 2724010380»). Чистить после apply:
  ```python
  if c.snils.startswith("ИНН "):
      c.inn = re.sub(r"\D","",c.snils)[:12]; c.snils = ""; c.save()
  ```
- **`apply_man` НЕ создаёт `ClientAddress`** из `raw.AdresR`/`postIndex`/`numbHouse`/`SubjectRF`. Адрес уходит в `Client.notes` или теряется.
- **`apply_correspondence`** мапит `response_text` из `raw.textResponce` — корректно. Поле `raw.answer` (краткий ответ) **не маппится** (теряется). Если важно — добавить отдельное поле или дописать в `response_text`.

## Сорorrespondence (запросы юристов в госорганы) — модель и UI-задел

Модель **`apps.crm.Correspondence`** уже в БД с миграцией, **33 780+ записей** имеется (на 4 июня 2026), мапится из Bubble `Сorrespondence` (с кириллической С — особенность Bubble) через `apply_correspondence`. Поля Siri-модели:

| Поле | Тип | Назначение |
|---|---|---|
| `service` | FK Service | К какой услуге привязан запрос |
| `counterparty` | FK LegalEntity | Госорган-адресат |
| `direction` | choices (outgoing/incoming) | Исходящее/входящее |
| `subject_type` | CharField | «Запрос в Росреестр», «Исковое заявление» и т.п. |
| `outgoing_number` | CharField | Исходящий номер |
| `sent_at` | DateField | Дата отправки |
| `delivery_method` | choices (post/email/site/telegram/courier) | Способ отправки |
| `file_link` | TextField | URL GDrive/S3 на сам документ |
| `track_response` | bool | Нужно ли контролировать ответ |
| `control_date` | DateField | До какого числа ждать ответа |
| `response_received` | bool | Ответ пришёл |
| `response_date` | DateField | Дата ответа |
| `response_text` | TextField | Текст ответа |
| `response_number` | CharField | Входящий номер ответа |
| `comments` | TextField | Комментарии юриста |

В docstring модели: «UI добавим отдельно — пока работа только через админку». **UI ещё нет.** Сделать список запросов по услуге (с подсветкой просроченных по `control_date`), форму создания, форму ввода ответа, виджет «на контроле» в дашборде юриста. Это завершит миграцию юрист-отдела с Bubble на Siri.

Bubble-маппинг (TypeCorrespondence — справочник 25 типов запросов: Росреестр, ИФНС, МРЭО, ПФР, банки, приставы, ходатайства, исковые, договоры). Эти типы импортируются в `subject_type` как строки. Если нужен **справочник в Siri** для UI-выбора — пока его нет, есть только distinct'ы по subject_type существующих записей.
