# Проверить nginx
sudo nginx -t
sudo systemctl status nginx

# Посмотреть SSL сертификаты
sudo certbot certificates

# Обновить SSL сертификаты (автоматически)
sudo certbot renew --dry-run