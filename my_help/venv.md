# создать виртульное окружение
python -m venv venv
# активировать виртуальное окружение
source venv/bin/activate

# установить зависимости 
pip install -r requirements.txt

# сохранить зависимости
pip freeze > requirements.txt

# обновить pip
python -m pip install --upgrade pip