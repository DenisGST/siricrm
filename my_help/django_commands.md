# создать проект
django-admin startproject config .

# Создать приложения

python manage.py startapp core
python manage.py startapp crm
python manage.py startapp auth_telegram
python manage.py startapp telegram
python manage.py startapp storage
# миграции
python manage.py makemigrations
python manage.py migrate
# создать админа
python manage.py createsuperuser
#### запустить сервер
python manage.py runserver 0.0.0.0:8000

## убить все джанги - рунсервер
pkill -9 -f runserver
