# todo — что осталось / куда вернуться

Список незавершённых задач. Каждый пункт: **статус** · что уже сделано · что осталось · файлы/команды.

Метки приоритета: 🔴 надо сегодня · 🟡 ближайшее время · 🟢 при случае.

Ревизия: **06.07.2026**.

---

## 🟡 Telegram: webhook вместо polling, два бота

**Контекст:** сейчас один бот делает всё — polling (`poll_monitor_bot`) выключает
webhook, `channel_post` (лиды) тихо теряются из `allowed_updates`. На проде
`TELEGRAM_BOT_TOKEN` вообще отдаёт 401 (протухший).

**Целевая схема — два бота:**
- **`@Sirius_system_bot`** → лиды через **webhook на проде**
- **`@FOUSirius_bot`** → монитор/алёрты через **polling с dev**

**⏸ Ждёт от тебя:** свежий токен `@FOUSirius_bot` от BotFather.

**После получения токена:**

1. **Эксперимент на dev** — повесить webhook у `@Sirius_system_bot` на
   `https://crmsiri.ru/telegram/leads-webhook/<secret>/`, попросить тебя написать
   боту в личку, через 30 сек глянуть `getWebhookInfo.last_error_message`.
   - `null` → VPN не ломает webhook, идём настраивать прод напрямую.
   - `timeout` → нужен policy routing на хосте (CONNMARK + ip rule).

2. **Правка кода:**
   - `apps/telegram/leads_bot.py:BOT_TOKEN` → `TELEGRAM_LEADS_BOT_TOKEN`
   - `apps/core/tasks.py:poll_monitor_bot, monitor_health, monitor_vpn, daily_health_report` → `TELEGRAM_MONITOR_BOT_TOKEN`
   - `apps/telegram/management/commands/setup_telegram_leads_webhook` → `TELEGRAM_LEADS_BOT_TOKEN`

3. **ENV:**
   - `.env.prod`: убрать мёртвый `TELEGRAM_BOT_TOKEN`, добавить
     `TELEGRAM_LEADS_BOT_TOKEN`, `TELEGRAM_LEADS_CHANNEL_ID=-1003960014349`,
     `TELEGRAM_LEADS_WEBHOOK_SECRET`, `TELEGRAM_WEBHOOK_URL`.
   - `.env.dev`: добавить `TELEGRAM_MONITOR_BOT_TOKEN` (новый бот).

4. **Активация:** `setup_telegram_leads_webhook` на проде, выключить
   `PeriodicTask "poll-telegram-leads"`, тестовый лид в канал.

5. **Cleanup:** удалить DNS A-запись `telegram.siricrm.ru` (webhook вешаем
   на главный домен), обновить `CLAUDE.md` про новую архитектуру.

**Файлы:** `apps/telegram/leads_bot.py`, `apps/telegram/management/commands/setup_telegram_leads_webhook.py`,
`apps/core/tasks.py`, `.env.prod`, `.env.dev`.

---

## 🟡 Автоопределение адресата запросов + конверты — деплой (на dev, не выкачено)

**Сделано на dev (не коммичено, не задеплоено; ветка feat/production-ready):**
Автоподбор госоргана-адресата в запросах (`apps/procedure`) по типу+региону/адресу
клиента + запомненные правила, модалка адресатов пакета, конверты Почты РФ,
импорт ОСФР+Гостехнадзор.

**Шаги при выкатке на прод:**
1. `deploy` (миграции авто): `crm 0096/0097` (LegalEntity.ifns_code + бэкфилл из
   notes — на проде ФНС те же), `procedure 0016/0017` (RequestType.recipient_kind/
   recipient_lookup + разметка каталога), новая модель `RecipientRule`. Новых
   зависимостей нет — именно `deploy`, не `rebuild`. Конверты — на reportlab
   (LibreOffice не нужен).
2. **dev→prod sync справочников СФР/Гостехнадзор** (на проде DaData НЕ гонять —
   квота): `dumpdata` видов `LegalEntityKind` (СФР, Гостехнадзор) + их
   `LegalEntity` (kind СФР=79, Гостехнадзор=72) → scp → `loaddata` на проде
   (сохраняет UUID). Пример:
   ```
   # на dev:
   docker exec siricrm-web-1 python manage.py dumpdata crm.LegalEntityKind \
     --pks <uuid_СФР>,<uuid_Гостех> > /tmp/kinds.json
   docker exec siricrm-web-1 python manage.py shell -c "…serialize LegalEntity filter kind__short_name in (СФР,Гостехнадзор)…" > /tmp/osfr_gosteh.json
   # scp на прод, затем loaddata обоих файлов
   ```
3. **На проде** после sync: `python manage.py wire_recipient_kinds` — привяжет
   `req_sfr`→СФР, `req_gostehnadzor`→Гостехнадзор (миграция 0017 их не привязала,
   т.к. видов ещё не было). Без DaData, идемпотентно.
4. **Почтовый индекс госорганов** (`LegalEntity.postal_code`, миграция crm 0098):
   у МРЭО в адресе индекса нет → на конверте пусто. Добито на dev командой
   `enrich_legal_entity_postal_code --kinds МРЭО` (DaData clean/address, ~3151).
   🛑 На проде DaData НЕ гонять — перенести `postal_code` в составе МРЭО-записей
   dev→prod (dumpdata/loaddata LegalEntity kind=МРЭО, или update только колонки
   postal_code по pk). Прочие виды (ФНС/ЗАГС/суд/ОСФР/ГИМС/ДМИ) индекс имеют в
   адресе — им enrich не нужен.
5. Рестарт `web`/`celery` (шаблоны кэшируются cached.Loader). Юзеру — `Ctrl+Shift+R`.

**Осталось (Фаза 3b, отложено):** импорт **ЦЗН** (центры занятости, районного
уровня — с трудвсем/открытых данных, не DaData). Пока req_employment на ручном
вводе. Волгоград (и ~18 регионов) без гостехнадзора в DaData — тоже ручной ввод
(орган назван иначе, напр. комитет сельского хозяйства); добавляются вручную и
запоминаются правилом.

**Файлы:** `apps/procedure/{recipient_resolver,services,views,request_envelopes}.py`,
`apps/procedure/models.py` (RequestType поля, RecipientRule), `apps/afd/envelope.py`
(party_from_request), `apps/crm/management/commands/{import_osfr,import_gostehnadzor}.py`,
`apps/procedure/management/commands/wire_recipient_kinds.py`, шаблоны корреспонденции/пакета/типа.

---

## 🟢 Дедуп задвоенных папок файл-менеджера клиентов

**Контекст (01.07.2026):** у ~118 клиентов задвоены дефолтные `ClientFolder`
(две `slug="root"` ×118; `chat`/`chat_sent`/`chat_received`/`personal` ×117;
+ ~900 дублей с пустым slug у именованных папок) — вероятно после объединения
карточек (`apps/crm/client_merge.py`). Из-за этого `get_or_create` в
`apps/files/folder_utils.py:_mk` падал `MultipleObjectsReturned` → ломало
прикрепление файлов: генерация договора/заявления о банкротстве, привязка сканов.

**Сделано:** `_mk` теперь устойчив к дублям — берёт самую старую подходящую
папку, иначе создаёт (коммит `1797a56`, на проде). Генерация работает.

**Осталось (очистка данных, не срочно):** слить дубли-деревья — для каждого
клиента с дублями перенести `ClientFile` и `children` из младших папок в
старейшую (по `client`+`slug`), удалить опустевшие дубли. Идемпотентная
management-команда с `--dry-run`.

**Файлы:** `apps/files/folder_utils.py`, `apps/files/models.py`,
новая `apps/files/management/commands/dedupe_client_folders.py`.
Память: [`duplicate-client-folders`](../../root/.claude/projects/-var-www-siricrm/memory/duplicate-client-folders.md).

---

## 🟢 Disk usage handler — кнопка в дашборде DevOps

Handler `apps/devops/handlers/disk_usage.py` реализован, `ActionType.DISK_USAGE`
в `models.py`, choice в миграциях, зарегистрирован в `__init__.py`. Через
API (`POST /devops/run/<env_id>/disk_usage/`) работает. **Не хватает только
кнопки** в `templates/devops/dashboard.html` (в секцию S3 или отдельной
строкой «📁 Диск: разбивка»).

**Файлы:** `templates/devops/dashboard.html`.

---

## 🟢 Прод-backup на NAS — disaster recovery

Из памяти [`prod-backup-strategy-pending`](../../root/.claude/projects/-var-www-siricrm/memory/prod-backup-strategy-pending.md).
Два варианта: `rclone+rsync` vs `restic`. Решение откладывается на выходные.

---

## Что НЕ входит в этот файл

- Чужие WIP в working tree — задачи других сессий, ведут их авторы.
- Userbot (`apps/telegram/userbot.py`) — явное указание «не трогать».

---

## Недавно закрыто (для истории, чтоб не искать)

| Дата | Что | Как решилось |
|---|---|---|
| 06.07 | Zombie-процессы (web=20 gpg-детей LibreOffice + celery=48) | Коммит `9edea8e` — `init: true` в compose для web/celery/celery-beat/userbot/devops-runner. Docker ставит tini как PID 1, reap автоматически. После `up -d --force-recreate` на проде — **0 zombie**. Arbitr не трогали (в отдельной ветке). |
| 06.07 | Логи celery: transient 502 при deploy + TG long-poll timeout как ERROR | Коммит `94ee55a` — понижено до INFO с прицельным catch (`AgentError.status in (502,503,504)` и `requests.ReadTimeout`). |
| 06.07 | `Received unregistered task: daily_health_report` на проде | Рестарт `devops-runner` на проде — подхватил новый код (был Up 13 дней) |
| 30.06 | PROD `claude0` мёртв 12+ часов | Сам восстановился (handshake 16s ago на 06.07) + добавлен `awg1` + failover |
| 30.06 | VPN на проде: только один канал `claude0` | Установлен `amneziawg` (PPA + DKMS), поднят `awg1`, `awg-failover.service` активен |
| 25.06 | Send-таски: `is_failed=True` при exhaustion | Коммит `a219268` — is_failed в MaxRetriesExceededError для WA/MAX/TG |
| 25.06 | `retry_stuck_messages` команда | Коммит `a219268` — с защитами 5min…24h + employee-not-null |
| 25.06 | Ротация локальных `backups/` | Коммит `a219268` — `_rotate_local_backups` в `backup.py`, env `LOCAL_BACKUP_RETENTION_DAYS=14` |
| 24.06 | DNS-фолбэк на проде | `/etc/resolv.conf` 3 nameserver + compose `dns:` через YAML anchor |
| 24.06 | `apps.efrsb` — rollback снят | Коммит `4c0abae` — модуль закоммичен, HEAD прода = `fda40a8` |
