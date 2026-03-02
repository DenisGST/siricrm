### Рекомендации на будущее:
Настрой автоматические бэкапы БД:

bash
# Создай скрипт backup.sh
nano /root/backup.sh
bash
#!/bin/bash
docker exec siricrm_db_1 pg_dump -U crm_user crm_db | gzip > /root/backups/db_$(date +%Y%m%d_%H%M%S).sql.gz
find /root/backups/ -name "db_*.sql.gz" -mtime +7 -delete
bash
chmod +x /root/backup.sh
mkdir -p /root/backups
crontab -e
# Добавь: 0 3 * * * /root/backup.sh
Мониторинг логов:

bash
docker-compose logs -f --tail=50
Обновление системы:

bash
sudo apt update && sudo apt upgrade -y
docker-compose pull && docker-compose up -d