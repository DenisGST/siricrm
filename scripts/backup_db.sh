#!/usr/bin/env bash
# =============================================================================
# Бэкап БД SiriCRM: pg_dump → gzip → S3 + локальная копия с ротацией
#
# Используется в docker-compose.prod.yml (контейнер backup).
# Также можно запустить вручную: ./scripts/backup_db.sh
# =============================================================================
set -euo pipefail

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
LOCAL_FILE="${BACKUP_DIR}/db-${TIMESTAMP}.sql.gz"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting backup..."

# 1. pg_dump → gzip → локальный файл
PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
    -h "${POSTGRES_HOST:-db}" \
    -U "${POSTGRES_USER}" \
    -d "${POSTGRES_DB}" \
    --no-owner --no-acl \
    | gzip > "$LOCAL_FILE"

SIZE=$(du -h "$LOCAL_FILE" | cut -f1)
echo "[$(date)] Local backup created: $LOCAL_FILE ($SIZE)"

# 2. Загрузка в S3 (если задан AWS_BACKUP_BUCKET_NAME)
if [ -n "${AWS_BACKUP_BUCKET_NAME:-}" ]; then
    python /app/scripts/upload_backup_s3.py "$LOCAL_FILE" || \
        echo "[WARN] S3 upload failed, but local backup is OK"
fi

# 3. Ротация: удаляем локальные бэкапы старше N дней
find "$BACKUP_DIR" -name "db-*.sql.gz" -mtime +${RETENTION_DAYS} -delete
echo "[$(date)] Rotation done (kept ${RETENTION_DAYS} days)"

echo "[$(date)] Backup finished successfully"
