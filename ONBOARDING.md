# SiriCRM — Onboarding

Быстрый старт для разработчика (или для себя на новой машине).

## Что это

CRM для юрфирмы (банкротство физлиц). Django 5.2 + HTMX + daisyUI + Celery + Channels (WS). Интеграции: Telegram, MaxChat, Beget S3, DaData. Подробности — в `CLAUDE.md`.

## Серверы

| Окружение | Сервер | Домен | Назначение |
| --------- | ------ | ----- | ---------- |
| **dev**  | 5.35.94.218  | crmsiri.ru | разработка ведётся здесь |
| **prod** | 45.90.35.187 | siricrm.ru | боевой — не трогать без необходимости |

Разработка идёт **на dev-сервере**. Подключение — по SSH с любого локального компа.

## Начать работу

```bash
# 1. Подключиться к dev-серверу
ssh root@5.35.94.218

# 2. Перейти в проект
cd /var/www/siricrm

# 3. Обновить код
git checkout feat/production-ready   # или main
git pull

# 4. (первый раз на сервере) Запустить Claude Code и аутентифицироваться
claude
```

Стек уже запущен (контейнеры с `restart: always`). Проверить:
```bash
docker compose -f docker-compose.prod.yml --env-file .env.dev ps
curl https://crmsiri.ru/health/        # {"status":"ok","db":true,"redis":true}
```

## Частые команды

```bash
# логи
docker compose -f docker-compose.prod.yml --env-file .env.dev logs -f --tail=100 web

# django shell / миграции
docker compose -f docker-compose.prod.yml --env-file .env.dev exec web python manage.py shell
docker compose -f docker-compose.prod.yml --env-file .env.dev exec web python manage.py migrate

# перезапуск web (после изменения кода — collectstatic+migrate выполняются автоматически)
docker compose -f docker-compose.prod.yml --env-file .env.dev restart web
docker compose -f docker-compose.prod.yml --env-file .env.dev restart nginx   # если web пересоздавался

# проверка перед коммитом
docker compose -f docker-compose.prod.yml --env-file .env.dev exec -T web python manage.py check
```

## DevOps-панель

`https://crmsiri.ru/devops/` (нужен суперюзер) — кнопки: Status / Backup / List backups / Pull prod→dev / Deploy / Rebuild. Управляет prod-сервером через HTTP-агента. Опасные операции требуют ввода слова-подтверждения.

## Деплой на prod

Через DevOps-панель (кнопка Deploy/Rebuild) или вручную — см. `docs/PRODUCTION.md`.

## Где что

- `apps/` — приложения (core, crm, files, realtime, telegram, maxchat, consultations, questionnaire, devops)
- `config/settings/` — base.py + dev.py + prod.py (переключение через `DJANGO_ENV`)
- `templates/` — Django-шаблоны
- `docs/` — `PRODUCTION.md`, `DEV_MIGRATION.md`, `legacy-quickstart.md`
- `CLAUDE.md` — контекст для Claude Code (читать в первую очередь)

## Важные грабли

- Секреты — только в `.env.prod` / `.env.dev` (в `.gitignore`, шаблоны — `.env.*.example`). Никогда не коммитить.
- `docker compose restart` НЕ перечитывает `env_file` — для смены env нужен `up -d --force-recreate`.
- VPN (Amnezia/WireGuard) на серверах: split-tunnel для Telegram + Anthropic. НЕ ставить `AllowedIPs=0.0.0.0/0` — порвётся SSH.
- Beget S3 валится на boto3 PUT — загрузка только через pre-signed URL.
- Проект НЕ использует `base.html` — `dashboard.html` самодостаточен.
