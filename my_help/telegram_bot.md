### Получить API credentials
Зайди на https://my.telegram.org/apps

Логинься своим номером (тем, с которого будет работать userbot)

Создай приложение, получи api_id и api_hash

Добавь в .env
text
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef
TELEGRAM_PHONE=+79991234567

В settings.py добавь:

python
TELEGRAM_API_ID = env.int("TELEGRAM_API_ID")
TELEGRAM_API_HASH = env.str("TELEGRAM_API_HASH")
TELEGRAM_PHONE = env.str("TELEGRAM_PHONE")