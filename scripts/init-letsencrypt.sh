#!/usr/bin/env bash
# =============================================================================
# Инициализация SSL Let's Encrypt при первом запуске.
#
# Перед запуском убедиться:
#   - DNS A-запись (NGINX_HOST) → IP сервера уже распространилась
#   - .env.{prod|dev} заполнен и содержит NGINX_HOST, NGINX_HOST_WWW, LETSENCRYPT_EMAIL
#   - порты 80/443 открыты в firewall
#
# Использование:
#   ENV_FILE=.env.prod ./scripts/init-letsencrypt.sh        # боевой
#   ENV_FILE=.env.dev LETSENCRYPT_STAGING=1 ./scripts/init-letsencrypt.sh  # тест
# =============================================================================
set -euo pipefail

ENV_FILE="${ENV_FILE:-.env.prod}"
if [ ! -f "$ENV_FILE" ]; then
    echo "[ERROR] $ENV_FILE not found"
    exit 1
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

PRIMARY="${NGINX_HOST:?NGINX_HOST must be set in $ENV_FILE}"
WWW="${NGINX_HOST_WWW:-www.$PRIMARY}"
EMAIL="${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL must be set in $ENV_FILE}"
STAGING="${LETSENCRYPT_STAGING:-0}"

COMPOSE="docker compose -f docker-compose.prod.yml --env-file $ENV_FILE"

echo "### Домены: $PRIMARY, $WWW"
echo "### Email: $EMAIL"
echo "### Staging: $STAGING"

# 1. Создаём dummy-сертификат, чтобы nginx стартанул
echo "### Создаём временный сертификат ..."
$COMPOSE run --rm --entrypoint "\
    sh -c 'mkdir -p /etc/letsencrypt/live/$PRIMARY && \
    openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout /etc/letsencrypt/live/$PRIMARY/privkey.pem \
        -out /etc/letsencrypt/live/$PRIMARY/fullchain.pem \
        -subj /CN=localhost'" certbot

# 2. Запускаем nginx
echo "### Запускаем nginx с временным сертификатом ..."
$COMPOSE up --force-recreate -d nginx

# 3. Удаляем dummy и запрашиваем настоящий
echo "### Удаляем временный сертификат ..."
$COMPOSE run --rm --entrypoint "\
    rm -rf /etc/letsencrypt/live/$PRIMARY \
    /etc/letsencrypt/archive/$PRIMARY \
    /etc/letsencrypt/renewal/$PRIMARY.conf" certbot

STAGING_ARG=""
if [ "$STAGING" != "0" ]; then
    STAGING_ARG="--staging"
fi

echo "### Запрашиваем настоящий сертификат у Let's Encrypt ..."
$COMPOSE run --rm --entrypoint "\
    certbot certonly --webroot -w /var/www/certbot \
        $STAGING_ARG \
        --email $EMAIL \
        -d $PRIMARY -d $WWW \
        --rsa-key-size 4096 \
        --agree-tos \
        --force-renewal" certbot

# 4. Перезапускаем nginx
echo "### Перезапускаем nginx ..."
$COMPOSE exec nginx nginx -s reload

echo "### Готово! HTTPS работает на https://$PRIMARY"
