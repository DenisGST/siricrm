# SiriCRM — Production Deployment Guide

Документ по выводу SiriCRM в продакшн на сервере **45.90.35.187 (siricrm.ru)**.

## Архитектура

```
                    [Internet]
                       │ :443 / :80
                       ▼
                   [nginx] ── /static/ ──► (volume static_files)
                       │
                       │ :8000
                       ▼
                  [Daphne (ASGI: HTTP+WS)]
                       │
            ┌──────────┼──────────┐
            ▼          ▼          ▼
        [Postgres]  [Redis]  [Celery + Beat + Userbot]
                       │
                       └─► [Beget S3] (медиа + бэкапы)
```

## Карта окружений

| Окружение | Сервер     | Домен      | Бакет media           | Бакет backup          |
| --------- | ---------- | ---------- | --------------------- | --------------------- |
| **prod**  | 45.90.35.187 | siricrm.ru | (старый бакет)        | отдельный (создать)   |
| **dev**   | 5.35.94.218  | crmsiri.ru | `1464bbae4a12-siridev-s3` | тот же бакет     |

Один и тот же `docker-compose.prod.yml` запускается на обоих серверах — отличия только в `.env.prod` / `.env.dev`.

## Требования к серверу

- Ubuntu 22.04+
- Docker + docker compose
- Открытые порты: 22 (SSH), 80, 443
- DNS: `siricrm.ru` и `www.siricrm.ru` → 45.90.35.187 (A-запись)
- Минимум 2 GB RAM, 20 GB SSD

## Подготовка

### 1. DNS

A-записи `siricrm.ru` и `www.siricrm.ru` → 45.90.35.187.
Проверить: `dig siricrm.ru +short` должно вернуть IP.

### 2. Файрвол

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo apt install fail2ban -y
sudo systemctl enable --now fail2ban
```

### 3. Файл .env.prod

```bash
cp .env.prod.example .env.prod
nano .env.prod
```

Обязательные параметры:
- `SECRET_KEY` — `python -c "import secrets; print(secrets.token_urlsafe(50))"`
- `POSTGRES_PASSWORD` — сильный пароль
- `DATABASE_URL` — должен совпадать по паролю
- `AWS_*` — prod-бакет
- `AWS_BACKUP_BUCKET_NAME` — отдельный бакет для бэкапов
- `SENTRY_DSN` — из аккаунта Sentry
- `LETSENCRYPT_EMAIL` — для уведомлений о сертификатах

## Первый запуск

### Шаг 1. Применить миграции (без nginx)

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d db redis
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm web python manage.py migrate
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm web python manage.py createsuperuser
```

### Шаг 2. Получить SSL сертификат

Сначала тестовый (staging), чтобы не упереться в rate limit:
```bash
ENV_FILE=.env.prod LETSENCRYPT_STAGING=1 ./scripts/init-letsencrypt.sh
```

Если ОК — настоящий:
```bash
ENV_FILE=.env.prod LETSENCRYPT_STAGING=0 ./scripts/init-letsencrypt.sh
```

### Шаг 3. Запустить весь стек

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

Проверить:
```bash
curl https://siricrm.ru/health/
# {"status":"ok","db":true,"redis":true}
```

## Обновление кода (rolling deploy)

```bash
git pull
docker compose -f docker-compose.prod.yml --env-file .env.prod build web
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --no-deps web celery celery-beat
```

## Восстановление из бэкапа

```bash
# Локальный
gunzip -c backups/db-YYYYMMDD-HHMMSS.sql.gz | \
    docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T db psql -U crm_user -d crm_db
```

## Мониторинг

- **Sentry** — ошибки приложения автоматически
- **Healthcheck**: `https://siricrm.ru/health/` → подключить к UptimeRobot
- **Логи**:
  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f --tail=200 web
  docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f nginx
  ```

## Безопасность

- Никогда не коммитить `.env.prod`
- Ротировать `SECRET_KEY` и `POSTGRES_PASSWORD` раз в полгода
- SSH: только по ключу, парольный вход выключен
- Регулярно: `apt update && apt upgrade`
- Бэкапы автоматически в S3 (см. контейнер `backup`)
