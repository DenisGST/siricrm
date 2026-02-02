# Установка
curl -sSf https://get.openziti.io/install.bash | sudo bash -s zrok

# Запуск 
zrok share public http://localhost:8000/api/telegram/webhook/

# Изменить адрес вебхук в телеге
https://api.telegram.org/bot8446931203:AAEInJ7kDzNWoZATy0lO0xvA-yM2qkanmHM/setWebhook?url=https://sf805yylreea.share.zrok.io

# Изменить ALLOWED HOST


# Запуск через tmux

## Установить:
bash
apt install tmux

## Старт сессии:
bash
tmux

## Внутри tmux запускаешь всё, что нужно:
bash
source venv/bin/activate
python manage.py runserver 0.0.0.0:8000
./tailwindcss -i ./assets/css/input.css -o ./static/css/tailwind.css --watch

## Можно в разных окнах tmux (Ctrl+B, затем C — новое окно; Ctrl+B, затем N/P — переключение).

## Отсоединиться от сессии, не останавливая процессы:
Нажми: Ctrl+B, потом D.
## Потом снова подключиться:
bash
tmux attach
## (Если сессий несколько: tmux ls и tmux attach -t имя).

Для запуска туннеля в zrok необходимо выполнить три основных этапа: регистрацию, активацию окружения и сам запуск. 
1. Регистрация и вход
Если вы используете публичный инстанс (например, zrok.io), сначала создайте аккаунт и получите токен:
Зарегистрируйтесь на myzrok.io.
В терминале выполните команду для привязки вашей системы к аккаунту:
bash
zrok enable <ваш_секретный_токен>
Используйте код с осторожностью.

(Токен можно найти в веб-консоли zrok). 
2. Запуск туннеля
Команда зависит от того, какой доступ вам нужен: публичный (через интернет) или приватный (только для других пользователей zrok).
Публичный туннель (аналог ngrok):
Создает временный URL, доступный любому пользователю в интернете.
bash
zrok share public http://localhost:8080
Используйте код с осторожностью.

В ответе вы получите ссылку вида https://...share.zrok.io.
Приватный туннель:
Безопаснее, так как трафик идет внутри сети OpenZiti. Доступ по ссылке будет невозможен без установленного клиента zrok.
bash
zrok share private http://localhost:8080
Используйте код с осторожностью.

Для доступа к нему на другой машине нужно будет запустить:
bash
zrok access private <токен_доступа>
```.

Используйте код с осторожностью.

 
3. Дополнительные режимы
Доступ к файлам: Если нужно поделиться папкой как веб-сервером:
bash
zrok share public ./ваша_папка --backend-mode web
```.
Используйте код с осторожностью.

TCP-туннели: Для SSH или игровых серверов:
bash
zrok share private --backend-mode tcpTunnel localhost:22
```.
Используйте код с осторожностью.

Постоянный URL (Reserved): Чтобы ссылка не менялась при перезапуске, сначала зарезервируйте имя:
bash
zrok reserve public --unique-name my-cool-app localhost:8080
zrok share reserved my-cool-app
```.

Используйте код с осторожностью.

Как остановить: Нажмите Ctrl+C. По умолчанию туннели являются временными (ephemeral) и удаляются сразу после закрытия программы. 



