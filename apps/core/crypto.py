"""Симметричное шифрование секретов (Fernet) для хранения в БД.

Применяется к паролю ЕФРСБ сотрудника-АУ (`Employee.efrsb_password_enc`): в базе
лежит только шифртекст, расшифровать можно лишь имея ключ.

Ключ берём из `settings.EFRSB_CRED_KEY` (полноценный Fernet-ключ в .env), а если
он не задан — детерминированно выводим из `SECRET_KEY` (SHA-256 → urlsafe base64).
Второй путь работает без правки окружения. 🛑 Но если ключ выводится из SECRET_KEY,
то при его ротации уже сохранённые пароли перестанут расшифровываться (вернётся
пустая строка) — АУ просто вводят пароль заново. Для стабильности задайте
`EFRSB_CRED_KEY` в .env (сгенерировать: `python -c "from cryptography.fernet import
Fernet; print(Fernet.generate_key().decode())"`).
"""
import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = getattr(settings, "EFRSB_CRED_KEY", "") or ""
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)
    derived = base64.urlsafe_b64encode(
        hashlib.sha256((settings.SECRET_KEY or "").encode()).digest()
    )
    return Fernet(derived)


def encrypt_secret(raw: str) -> str:
    """Открытый текст → шифртекст (пустой вход → пустая строка)."""
    if not raw:
        return ""
    return _fernet().encrypt(raw.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Шифртекст → открытый текст. Битый/чужой токен → пустая строка."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""
