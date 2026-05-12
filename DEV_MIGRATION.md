# SiriCRM — Перенос dev-окружения на новый сервер

Перенос разработки с **45.90.35.187 (текущий)** → **5.35.94.218 (новый dev, crmsiri.ru)**.

## Что переносим

| Что | Откуда | Куда |
| --- | ------ | ---- |
| Код | git origin | git clone на новом сервере |
| База данных | pg_dump на 45.90.35.187 | pg_restore на 5.35.94.218 |
| .env (адаптированный) | вручную | `.env.dev` на 5.35.94.218 |
| Docker-стек | `docker-compose.prod.yml` | тот же файл, переменные другие |
| S3 | старый бакет (читаем) | новый бакет `1464bbae4a12-siridev-s3` (читаем+пишем) |
| Telegram session | — | НЕ переносим, заводим тестовый бот |

## Шаг 1. Подготовка нового сервера 5.35.94.218

### 1.1. Базовые пакеты + Docker

```bash
ssh root@5.35.94.218
apt update && apt upgrade -y
apt install -y curl git ufw fail2ban
curl -fsSL https://get.docker.com | sh
```

### 1.2. Файрвол

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
systemctl enable --now fail2ban
```

### 1.3. Пользователь deploy (опционально)

```bash
adduser deploy --disabled-password
usermod -aG docker deploy
mkdir /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
```

### 1.4. Клонируем репозиторий

```bash
mkdir -p /var/www
cd /var/www
git clone https://github.com/<user>/siricrm.git    # или ваш git remote
cd siricrm
git checkout feat/production-ready                  # ветка с prod-стеком
```

## Шаг 2. БД: бэкап на старом → restore на новом

### 2.1. На старом сервере (45.90.35.187)

```bash
cd /var/www/projects/siricrm
docker compose exec -T db pg_dump -U crm_user -d crm_db | gzip > /tmp/dev-migrate-$(date +%Y%m%d).sql.gz
scp /tmp/dev-migrate-*.sql.gz root@5.35.94.218:/tmp/
```

### 2.2. На новом сервере (5.35.94.218)

```bash
cd /var/www/siricrm
cp .env.dev.example .env.dev
nano .env.dev   # заполнить значения (см. ниже)

# Запустить только БД и Redis
docker compose -f docker-compose.prod.yml --env-file .env.dev up -d db redis

# Подождать пока БД готова
sleep 10

# Восстановить дамп
gunzip -c /tmp/dev-migrate-*.sql.gz | \
    docker compose -f docker-compose.prod.yml --env-file .env.dev exec -T db \
    psql -U crm_user -d crm_db
```

## Шаг 3. Заполнение .env.dev

Минимум, что нужно поменять относительно `.env.dev.example`:

```dotenv
SECRET_KEY=<сгенерировать новый>
POSTGRES_PASSWORD=<сильный пароль, тот же в DATABASE_URL>
DATABASE_URL=postgresql://crm_user:<пароль>@db:5432/crm_db

# S3 dev — все ключи из вашего нового бакета
AWS_ACCESS_KEY_ID=JCZD7I51FRR44FWUS6LW
AWS_SECRET_ACCESS_KEY=<тот секрет, что вам дали>
AWS_STORAGE_BUCKET_NAME=1464bbae4a12-siridev-s3
AWS_BACKUP_BUCKET_NAME=1464bbae4a12-siridev-s3

# Telegram — отдельный тестовый бот
TELEGRAM_BOT_TOKEN=<новый бот через @BotFather>
TELEGRAM_API_ID=<свой>
TELEGRAM_API_HASH=<свой>
TELEGRAM_PHONE=<отдельный тестовый номер ИЛИ оставить пустым>

# Sentry — можно тот же DSN с environment=development
SENTRY_DSN=<тот же или новый проект>
SENTRY_ENVIRONMENT=development
```

## Шаг 4. SSL для crmsiri.ru

```bash
# Тестовый сертификат
ENV_FILE=.env.dev LETSENCRYPT_STAGING=1 ./scripts/init-letsencrypt.sh
# Боевой сертификат
ENV_FILE=.env.dev LETSENCRYPT_STAGING=0 ./scripts/init-letsencrypt.sh
```

## Шаг 5. Запуск всего стека

```bash
docker compose -f docker-compose.prod.yml --env-file .env.dev up -d
```

Проверить:
```bash
curl https://crmsiri.ru/health/
# {"status":"ok","db":true,"redis":true}
```

## Шаг 6. Дальше — продолжаем разработку на 5.35.94.218

С этого момента:
- Старый сервер 45.90.35.187 — **трогать не нужно**, остаётся как был, продолжает работать
- Новая разработка ведётся на 5.35.94.218 (clone, push, pull, миграции)
- Когда придёт время продакшна — на 45.90.35.187 разворачиваем prod-стек по `PRODUCTION.md`

## Чего НЕ делать

- ❌ НЕ настраивать Telegram userbot на dev с боевыми сессиями (зайдёт два клиента в один аккаунт — конфликт)
- ❌ НЕ использовать тот же `SECRET_KEY`, что у будущего prod
- ❌ НЕ запускать оба сервера с одной БД-репликой одновременно (будет рассинхрон)
