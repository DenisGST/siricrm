"""Одноразовые коды привязки Telegram-аккаунта сотрудника к его профилю.

Зеркало `apps/maxchat/linkcode.py` — тот же флоу, свой namespace в Redis:
  1. Сотрудник в профиле жмёт «Получить код» → `issue(emp_id)` кладёт короткий
     код в Redis на TTL и возвращает его для показа.
  2. Сотрудник отправляет этот код нашему боту (@Sirius_system_bot).
  3. Поллер getUpdates (`apps/telegram/tasks._poll_once`) на каждое приватное
     сообщение зовёт `claim(text)`; если текст = активный код — возвращает
     employee_id, код гасится, а chat_id сохраняется в
     `Employee.telegram_chat_id`.

Хранилище — общий Redis-кэш Django (тот же, что и сессии), переживает рестарт
web-контейнера.
"""
from __future__ import annotations

import secrets

from django.core.cache import cache

CODE_TTL = 15 * 60  # 15 минут на ввод кода

_CODE_KEY = "tg:linkcode:code:{code}"   # code   -> employee_id
_EMP_KEY = "tg:linkcode:emp:{emp_id}"   # emp_id -> code (чтобы показать в профиле)

# Без визуально похожих символов (0/O, 1/I) — код диктуют/перепечатывают вручную.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_code(n: int = 6) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def issue(emp_id: int) -> str:
    """Выдать (перевыпустив прежний) код привязки для сотрудника."""
    emp_id = int(emp_id)
    old = cache.get(_EMP_KEY.format(emp_id=emp_id))
    if old:
        cache.delete(_CODE_KEY.format(code=old))
    code = _gen_code()
    cache.set(_CODE_KEY.format(code=code), emp_id, CODE_TTL)
    cache.set(_EMP_KEY.format(emp_id=emp_id), code, CODE_TTL)
    return code


def current(emp_id: int) -> str | None:
    """Текущий активный код сотрудника (для показа в профиле) или None."""
    return cache.get(_EMP_KEY.format(emp_id=int(emp_id)))


def claim(text: str) -> int | None:
    """Найти сотрудника по коду из входящего сообщения и погасить код.

    Возвращает employee_id либо None (если текст — не активный код).
    Нормализуем к верхнему регистру: алфавит кода в верхнем регистре.
    """
    code = (text or "").strip().upper()
    if not code or len(code) > 16:
        return None
    emp_id = cache.get(_CODE_KEY.format(code=code))
    if emp_id is None:
        return None
    cache.delete(_CODE_KEY.format(code=code))
    cache.delete(_EMP_KEY.format(emp_id=int(emp_id)))
    return int(emp_id)
