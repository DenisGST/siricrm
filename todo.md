# todo — что осталось / куда вернуться

Список незавершённых задач из текущих и прошлых сессий. Каждый пункт:
**статус** · что уже сделано · что осталось · файлы/команды.

Метки приоритета: 🔴 надо сегодня · 🟡 ближайшее время · 🟢 при случае.

---

## 🟢 Дедуп задвоенных папок файл-менеджера клиентов

**Контекст (01.07.2026):** у ~118 клиентов задвоены дефолтные `ClientFolder`
(две `slug="root"` ×118; `chat`/`chat_sent`/`chat_received`/`personal` ×117;
+ ~900 дублей с пустым slug у именованных папок) — вероятно после объединения
карточек (`apps/crm/client_merge.py`). Из-за этого `get_or_create` в
`apps/files/folder_utils.py:_mk` падал `MultipleObjectsReturned` → ломало
прикрепление файлов: генерация договора/заявления о банкротстве, привязка сканов
(жалоба: «Составить договор» для клиента Объедкова → внутренняя ошибка).

**Сделано:** `_mk` теперь устойчив к дублям — берёт самую старую подходящую
папку, иначе создаёт (коммит `1797a56`, на проде). Генерация работает, файлы
кладутся в канонический (старейший) корень.

**Осталось (очистка данных, не срочно):** слить дубли-деревья — для каждого
клиента с дублями перенести `ClientFile` и `children` из младших папок в
старейшую (по `client`+`slug`), удалить опустевшие дубли; проверить, что
файл-менеджер показывает канонический корень. Сделать идемпотентную
management-команду (напр. `dedupe_client_folders`) с `--dry-run`.

**Файлы:** `apps/files/folder_utils.py`, `apps/files/models.py`
(ClientFolder/ClientFile), новая `apps/files/management/commands/dedupe_client_folders.py`.
Память: `duplicate-client-folders`.

---

## 🟡 PROD: мёртвый `claude0` (backup-канал VPN) — решить через сутки

**Контекст:** 30.06.2026 на проде поднят `awg1` (AmneziaWG) + failover (по схеме dev).
В failover-цепочке `BACKUP=claude0` (старый плоский WG). Но в момент развёртывания
обнаружено: `claude0` уже **12+ часов без handshake**, peer `72.56.73.137:37539`
не отвечает на ping. То есть фолбэк нерабочий — если awg1 умрёт, переключаться
некуда (failover-скрипт безопасен: не переключится на дохлый, останется на awg1).

**Что наблюдаем:**
- `claude0` интерфейс UP, но peer не отвечает (`latest handshake: 12h ago`, `ping -I claude0` пусто)
- `awg1` живой и нагружен, трафик Telegram/Anthropic идёт через него

**Что сделать через сутки (01.07.2026):**
- Если `claude0` сам восстановился (handshake обновился) — оставить как есть, всё ок.
- Если по-прежнему мёртв — выбрать один из вариантов:
  - **(А) Снести claude0**: `systemctl disable --now wg-quick@claude0` + удалить `/etc/wireguard/claude0.conf`.
    Также убрать его из `awg-failover.sh` (поменять `BACKUP=claude0` на пустой или поднять второй
    Amnezia-канал как backup). Без фолбэка совсем — риск, что при сбое awg1 опять окажемся без VPN.
  - **(Б) Восстановить claude0**: запросить новый peer-endpoint у провайдера VPN, перенастроить
    `/etc/wireguard/claude0.conf` (новый Endpoint + ключи). Failover автоматически подхватит.
  - **(В) Поднять второй Amnezia-канал**: попросить у провайдера ещё один Amnezia-конфиг
    (другой endpoint), поднять как `awg2`, переключить failover на `BACKUP=awg2`. Самое чистое.

**Файлы:** `/etc/wireguard/claude0.conf` (на проде), `/usr/local/sbin/awg-failover.sh` (на проде).

---

## 🔴 apps.efrsb — снять временный rollback на проде

**Контекст:** 24.06 ~17:30 МСК после `up -d --force-recreate` web/celery-beat/arbitr-runner
ушли в restart-loop с `ModuleNotFoundError: No module named 'apps.efrsb'`. В
`config/settings/base.py:57` строка `"apps.efrsb"` уже в INSTALLED_APPS (закоммичено),
но папка `apps/efrsb/` — untracked в working tree на dev и на проде её нет.

**Что сделано экстренно:** на проде в `config/settings/base.py:57` строка
закомментирована с маркером `# ROLLBACK 24.06`. Бэкап — `config/settings/base.py.before-efrsb-rollback`.

**⚠️ Эта правка вне git** — следующий `git pull` через deploy/rebuild панели её затрёт
и прод снова упадёт.

**Нужно решить и закоммитить на dev один из двух:**

- **(A) Модуль готов к проду** — закоммитить `apps/efrsb/` (и все его untracked зависимости:
  `apps/afd/envelope.py`, `apps/afd/management/commands/envelope_demo.py`,
  `apps/afd/migrations/0007_alter_documenttemplate_kind.py`, `OLD/EFRSB/`), задеплоить.
  После deploy на проде вернуть строку — `sed -i 's|^.*# ROLLBACK 24.06.*$|    "apps.efrsb",|'
  config/settings/base.py` (или скопировать из бэкапа).

- **(B) Модуль ещё в разработке** — на dev убрать `"apps.efrsb",` из
  `config/settings/base.py:57`, закоммитить, запушить. После git pull на проде
  rollback станет no-op.

🛑 НЕЛЬЗЯ делать deploy/rebuild на проде до решения — упадёт повторно.

**Файлы:** `config/settings/base.py:57`, `apps/efrsb/*`.

---

## ✅ DNS-фолбэк на проде — ПРИМЕНЕНО 24.06 ~17:10 МСК

Применилось после flap'а Beget-резолвера после полудня. Команда:
`up -d --force-recreate celery celery-beat userbot web` (на prod-host compose).
В контейнерах теперь `# Overrides: [nameservers]` форвардит на 3 резервных
(не на Beget). 🛑 Потерянные входящие медиа (MAX/TG/WA) во время флапа —
не восстанавливаются (URL-токены CDN были только в логах вебхука).
Подробно — память [`dns-fallback-prod`](../../root/.claude/projects/-var-www-siricrm/memory/dns-fallback-prod.md).

---

## 🟡 Telegram: webhook вместо polling, два бота

**Контекст:** в текущей архитектуре путаница — один бот для всего, polling выключает webhook,
лиды (`channel_post`) тихо теряются в `poll_monitor_bot` (allowed_updates без channel_post).
На проде `TELEGRAM_BOT_TOKEN` отдаёт 401 (битый/протухший).

**Целевая схема:** разделить на **двух ботов**:
- **`@Sirius_system_bot`** → лиды через **webhook на проде**
- **`@FOUSirius_bot`** → монитор/алёрты через **polling с dev**

**Что нужно от пользователя:**
- Получить от BotFather свежий токен `@FOUSirius_bot` (новый бот, для монитора)
- Решить (если ещё не решено): какой URL у webhook'а — `siricrm.ru/telegram/leads-webhook/<secret>/`
  (просто, готово) или `telegram.siricrm.ru/...` (нужно настроить nginx + certbot)

**Что нужно сделать (после получения токена):**
1. Сначала эксперимент на dev: повесить временный webhook у `@Sirius_system_bot`
   на `https://crmsiri.ru/telegram/leads-webhook/<secret>/`, попросить пользователя
   написать боту, через 30 сек глянуть `getWebhookInfo.last_error_message`.
   - Если `null` — VPN не ломает webhook (split-tunnel и asymmetric routing — миф).
     Поехали настраивать прод напрямую.
   - Если timeout — нужен policy routing на хосте (iptables `CONNMARK` +
     `ip rule fwmark 1 table 100` + `ip route add default via $GW dev eth0 table 100`).
     Подробный план в обсуждении выше по треду — реализуется через `ssh siri-prod`.

2. Правка кода (после теста):
   - `apps/telegram/leads_bot.py:BOT_TOKEN` → читать `TELEGRAM_LEADS_BOT_TOKEN`
   - `apps/core/tasks.py:poll_monitor_bot, monitor_health` → читать `TELEGRAM_MONITOR_BOT_TOKEN`
   - `apps/telegram/management/commands/setup_telegram_leads_webhook` → читать `TELEGRAM_LEADS_BOT_TOKEN`
   - Старая `TELEGRAM_BOT_TOKEN` — оставить deprecation-fallback на пару деплоев.

3. ENV:
   - **`.env.prod`** — выкинуть мёртвый `TELEGRAM_BOT_TOKEN=8446931203:...` и `FOU_Sirius_Bot`
     username, добавить:
     ```
     TELEGRAM_LEADS_BOT_TOKEN=<токен @Sirius_system_bot — взять с dev .env.dev>
     TELEGRAM_LEADS_CHANNEL_ID=-1003960014349
     TELEGRAM_LEADS_WEBHOOK_SECRET=<с dev или новый openssl rand -hex 24>
     TELEGRAM_WEBHOOK_URL=https://siricrm.ru/telegram/leads-webhook/<secret>/
     ```
   - **`.env.dev`** — добавить:
     ```
     TELEGRAM_MONITOR_BOT_TOKEN=<новый токен @FOUSirius_bot>
     MONITOR_BOT_POLL=true   # как сейчас
     MONITOR_BOT_ALLOWED_CHAT_IDS=8796041453
     ```

4. После настройки прода:
   - На проде через `setup_telegram_leads_webhook` зарегистрировать webhook.
   - На dev — `MONITOR_BOT_POLL=true` остаётся, но через новый монитор-токен.
   - Тест — пользователь пишет тестовое сообщение в leads-канал в формате заявки,
     проверяем что в CRM на проде появился клиент.
   - Выключить ненужный polling: `PeriodicTask "poll-telegram-leads".enabled=False`.

5. После всего:
   - Удалить DNS A-запись `telegram.siricrm.ru` (если webhook вешали на главный домен) — пользователь.
   - Обновить `CLAUDE.md` / память `telegram-bot-split-tunnel`: «реальность — webhook на проде,
     polling только для монитора на dev, два разных бота».

**Файлы:** `apps/telegram/leads_bot.py`, `apps/telegram/management/commands/setup_telegram_leads_webhook.py`,
`apps/core/tasks.py`, `.env.prod`, `.env.dev`.

---

## 🟡 Send-таски: помечать `is_failed=True` после exhaustion ретраев

**Контекст:** 24.06 после DNS-сбоя 5 сообщений (3 WA + 2 MAX) повисли в
`is_sent=False AND is_failed=False`. В UI это значит «отправляется ⏳» вечно.
Сегодня уже ретрайнул вручную (все 5 ушли). Причина — Celery таски ретраят
3 раза × 10 сек, потом просто `logger.error("max retries exceeded")` без обновления Message.

**Что сделать:**

В `apps/whatsapp/tasks.py:send_whatsapp_message_task` и
`apps/maxchat/tasks.py:send_max_message_task` — в блоке retry, в обработке
`MaxRetriesExceededError`, проставлять `is_failed`:

```python
try:
    self.retry(exc=Exception(err or "send failed"))
except self.MaxRetriesExceededError:
    logger.error("send: max retries exceeded for msg %s", msg.id)
    msg.is_failed = True
    msg.error_text = (err or "max retries exceeded")[:500]
    msg.save(update_fields=["is_failed", "error_text"])
    try:
        from apps.realtime.utils import push_message_status
        push_message_status(msg)
    except Exception:
        pass
```

Заодно проверить, что `apps/whatsapp/tasks.py:send_whatsapp_template_task` уже так не делает —
если делает, унифицировать.

**Файлы:** `apps/whatsapp/tasks.py`, `apps/maxchat/tasks.py`.

---

## 🟡 Management-команда `retry_stuck_messages`

Чтобы в следующий раз не ходить руками. Найти все `is_sent=False AND is_failed=False
AND created_at < now - 5 минут` и переотправить через соответствующие таски.
Можно запустить вручную после сетевого сбоя или прицепить к Celery beat
(раз в 10 мин, dry-run mode по умолчанию).

```python
# apps/crm/management/commands/retry_stuck_messages.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from apps.crm.models import Message
from apps.whatsapp.tasks import send_whatsapp_message_task
from apps.maxchat.tasks import send_max_message_task
# ... TG task если есть

class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("--minutes", type=int, default=5)
        p.add_argument("--apply", action="store_true", help="без флага — dry run")

    def handle(self, *_, minutes, apply, **__):
        threshold = timezone.now() - timedelta(minutes=minutes)
        stuck = Message.objects.filter(
            direction="outgoing",
            is_sent=False,
            is_failed=False,
            created_at__lt=threshold,
        )
        ...
```

**Файлы:** новый `apps/crm/management/commands/retry_stuck_messages.py`.

---

## 🟢 Disk usage handler — кнопка в дашборде DevOps

**Контекст:** handler `apps/devops/handlers/disk_usage.py` уже реализован в прошлой
сессии, `ActionType.DISK_USAGE` есть в `models.py`, миграции 0007/0008 уже включают
choice. В __init__.py зарегистрирован. **Через API панели (POST `/devops/run/<env_id>/disk_usage/`)
работает.**

**Что осталось:** добавить **кнопку** в `templates/devops/dashboard.html` (рядом с
«Бакеты», скорее всего в секцию S3 или новой строкой «📁 Диск: разбивка»),
чтобы пользователь мог запустить через UI.

**Файлы:** `templates/devops/dashboard.html`.

---

## 🟡 Ротация локальных backups/ — диск растёт

**Контекст:** 24.06 после серии `pull_db`/`backup` через DevOps-панель
`/var/www/projects/siricrm/backups/` весит **7.2 GB**, диск на проде дошёл до 90%
(86G/96G, осталось 9.9G). Локальные дампы (`db-YYYYMMDD-HHMMSS.sql.gz`)
не ротируются — handler-ы `backup`, `pull_db`, `restore_db` только дописывают
в каталог, никто не чистит.

**Что сделать (любой из вариантов):**
- В `apps/devops/handlers/backup.py` после удачного бэкапа подчищать всё, что
  старше N дней (например, 14):
  ```python
  cutoff = time.time() - 14 * 86400
  for p in BACKUP_DIR.glob("db-*.sql.gz"):
      if p.stat().st_mtime < cutoff:
          p.unlink()
  ```
- Или отдельный handler `cleanup_local_backups` + кнопка в DevOps-панели.
- Или системный cron на хосте: `find /var/www/projects/siricrm/backups -name 'db-*.sql.gz' -mtime +14 -delete`.

В S3 уже лежат все дампы (через `s3_backup_key` в каждом backup-action) — локальный
кэш можно держать коротким (1-2 недели).

**Файлы:** `apps/devops/handlers/backup.py` (или новый `cleanup_local_backups.py`).

---

## 🟢 Прод-backup на NAS — disaster recovery

Из памяти [`prod-backup-strategy-pending`](../../root/.claude/projects/-var-www-siricrm/memory/prod-backup-strategy-pending.md).
Два варианта: `rclone+rsync` vs `restic`. Решение откладывается на выходные.

---

## Не входит в этот файл

- Чужие WIP в working tree (apps/efrsb/, apps/procedure/, apps/afd/envelope.py, и т.д.) —
  это задачи других сессий, ведут их авторы.
- Userbot (`apps/telegram/userbot.py`) — явное указание пользователя «не трогать».
