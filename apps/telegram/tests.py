"""Парсер заявок из Telegram-канала лидов (`apps.telegram.leads_bot`).

Сообщения — реальные образцы из канала (два формата, см. docstring модуля).
DB не нужна: парсер — чистые функции, поэтому SimpleTestCase.
"""
from django.test import SimpleTestCase

from .leads_bot import _parse_lead, _normalize_phone

CHANNEL_MSG = """🌐 Сайт: https://xn----ftbcrnpcej.xn--p1ai/
🔴 Новая заявка · Кредиты и долги
Источник: Обратный звонок после первого экрана
Лендинг: Кредиты и долги
Телефон: +79090751580
Связаться: Позвонить по телефону
IP: 37.113.171.177
Устройство: Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5.2 Mobile/15E148 Safari/604.1
Время: 23.08.2026 16:39
ID: 5"""

CHANNEL_MSG_2 = """🌐 Сайт: https://xn--80ablwldipt.xn--p1ai/
🔴 Новая заявка · Банкротство
Источник: Обратный звонок после первого экрана
Лендинг: Банкротство физических лиц
Телефон: +79063313802
Связаться: Позвонить по телефону
IP: 176.52.78.94
Устройство: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 YaBrowser/26.8.0.0 Safari/537.36
Время: 23.08.2026 22:02
ID: 6"""

FLEXBE_MSG = """Новая заявка № 1234 со страницы сириус-бфл.рф/clip-3n/
Название формы: Квиз
Данные формы:
Имя: Иван
Телефон: 8 (909) 075-15-80
Сумма долга: 800 000
Просмотр заявки (https://flexbe.ru/leads/1234)"""

# Квиз sirius-bfl.ru — самый насыщенный вариант канального формата
# (образец из боевого канала 27.08.2026): есть «Имя» и ответы квиза.
QUIZ_MSG = """🌐 Сайт: https://sirius-bfl.ru/
🔴 Новая заявка · Стоимость
Источник: Квиз
Лендинг: Стоимость банкротства
Имя: Патимат
Телефон: +79884216224
Регион: Дагестан
Сумма долга: До 300 000 ₽
Кредиторов: 3–5
Исполнительные производства: Нет
Имущество и залоги: Нет имущества
Ипотека: Нет
Источники дохода: Пенсия
Связаться: Написать в Telegram
IP: 85.26.176.221
Устройство: Mozilla/5.0 (Linux; arm_64; Android 13; SM-A325F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.7871.156 YaApp_Android/26.80.1 Mobile Safari/537.36
Время: 27.08.2026 12:30
ID: 20"""


class ParseChannelLeadTests(SimpleTestCase):
    def test_основные_поля(self):
        d = _parse_lead(CHANNEL_MSG)
        self.assertIsNotNone(d)
        self.assertEqual(d["phone"], "79090751580")
        self.assertEqual(d["number"], "5")
        self.assertEqual(d["form"], "Кредиты и долги")
        self.assertEqual(d["page"], "про-долги.рф")  # punycode → кириллица
        self.assertEqual(d["link"], "https://xn----ftbcrnpcej.xn--p1ai/")
        self.assertEqual(d["name"], "")  # ФИО в этом формате нет

    def test_второй_сайт(self):
        d = _parse_lead(CHANNEL_MSG_2)
        self.assertEqual(d["phone"], "79063313802")
        self.assertEqual(d["page"], "небанкрот.рф")
        self.assertEqual(d["form"], "Банкротство")
        self.assertEqual(d["number"], "6")

    def test_данные_заявки_без_тех_шума(self):
        answers = dict(_parse_lead(CHANNEL_MSG)["answers"])
        self.assertEqual(answers["Сайт"], "про-долги.рф")
        self.assertEqual(answers["Источник"], "Обратный звонок после первого экрана")
        self.assertEqual(answers["Лендинг"], "Кредиты и долги")
        self.assertEqual(answers["Связаться"], "Позвонить по телефону")
        self.assertEqual(answers["Время"], "23.08.2026 16:39")  # двоеточие в значении
        # Телефон/ID уже разобраны, UA — шум в карточке лида.
        for k in ("Телефон", "ID", "Устройство"):
            self.assertNotIn(k, answers)

    def test_не_заявка(self):
        self.assertIsNone(_parse_lead("Привет, как дела?"))
        self.assertIsNone(_parse_lead(""))


class ParseQuizLeadTests(SimpleTestCase):
    """Квиз: появилось «Имя» и произвольный набор ответов."""

    def test_имя_и_телефон(self):
        d = _parse_lead(QUIZ_MSG)
        self.assertEqual(d["name"], "Патимат")
        self.assertEqual(d["phone"], "79884216224")
        self.assertEqual(d["number"], "20")
        self.assertEqual(d["form"], "Стоимость")
        self.assertEqual(d["page"], "sirius-bfl.ru")

    def test_ответы_квиза_доезжают_целиком(self):
        answers = dict(_parse_lead(QUIZ_MSG)["answers"])
        self.assertEqual(answers["Регион"], "Дагестан")
        self.assertEqual(answers["Сумма долга"], "До 300 000 ₽")
        self.assertEqual(answers["Кредиторов"], "3–5")
        self.assertEqual(answers["Исполнительные производства"], "Нет")
        self.assertEqual(answers["Имущество и залоги"], "Нет имущества")
        self.assertEqual(answers["Источники дохода"], "Пенсия")
        self.assertEqual(answers["Связаться"], "Написать в Telegram")
        # «Имя» ушло в ФИО клиента, дублировать его в ответах незачем.
        self.assertNotIn("Имя", answers)
        self.assertNotIn("Устройство", answers)


class ParseFlexbeLeadTests(SimpleTestCase):
    def test_старый_формат_не_сломан(self):
        d = _parse_lead(FLEXBE_MSG)
        self.assertEqual(d["name"], "Иван")
        self.assertEqual(d["phone"], "79090751580")
        self.assertEqual(d["number"], "1234")
        self.assertEqual(d["form"], "Квиз")
        self.assertEqual(d["page"], "сириус-бфл.рф/clip-3n/")
        self.assertEqual(d["link"], "https://flexbe.ru/leads/1234")
        self.assertIn(("Сумма долга", "800 000"), d["answers"])


class NormalizePhoneTests(SimpleTestCase):
    def test_форматы(self):
        self.assertEqual(_normalize_phone("+7 909 075-15-80"), "79090751580")
        self.assertEqual(_normalize_phone("8 (909) 075-15-80"), "79090751580")
        self.assertEqual(_normalize_phone("9090751580"), "79090751580")
        self.assertEqual(_normalize_phone("123"), "")
