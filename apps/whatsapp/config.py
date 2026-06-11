"""Конфигурация интеграции с 1msg.io WhatsApp Business API.

Все значения берутся из env (см. ``.env.dev`` / ``.env.prod``). Один номер —
один инстанс, поэтому модель в БД не нужна; если в будущем потребуется
несколько — переезжаем на ``WhatsAppInstance``.

Боевой номер — на этапе пилота включён ``WHATSAPP_TEST_MODE=true``, тогда:
* входящий webhook от чужого номера → 200, но без записи в БД (только лог);
* исходящие сообщения на чужой номер → таска не отправляет (отказ).

Для разрешённых номеров (``WHATSAPP_ALLOWED_PHONES``, CSV в E.164 без +)
интеграция работает в полную силу.
"""
from decouple import config


def _csv_to_set(s: str) -> set[str]:
    """'79991234567, 79007778899' → {'79991234567', '79007778899'}."""
    return {x.strip() for x in (s or "").split(",") if x.strip()}


# Инстанс 1msg.io (channel)
INSTANCE_ID = config("WHATSAPP_INSTANCE_ID", default="")
API_TOKEN = config("WHATSAPP_API_TOKEN", default="")
PHONE = config("WHATSAPP_PHONE", default="")
API_BASE = config("WHATSAPP_API_BASE", default="https://api.1msg.io")

# Namespace WABA-шаблонов (общий для всех шаблонов инстанса). Нужен для
# sendTemplate. Стабилен в рамках одного WABA; если пусто — берётся из
# первого шаблона через sender.get_namespace().
NAMESPACE = config("WHATSAPP_NAMESPACE", default="991ceaad_9bf3_4128_b815_54d706ed24a4")

# Защита от случайной массовой отправки на боевой номер при разработке.
TEST_MODE = config("WHATSAPP_TEST_MODE", default=True, cast=bool)
ALLOWED_PHONES = _csv_to_set(config("WHATSAPP_ALLOWED_PHONES", default=""))

# Опциональный shared-secret для верификации webhook (передаётся в URL/header
# из кабинета 1msg). Если пусто — верификация отключена.
WEBHOOK_SECRET = config("WHATSAPP_WEBHOOK_SECRET", default="")


def is_configured() -> bool:
    return bool(INSTANCE_ID and API_TOKEN)


def is_phone_allowed(phone: str) -> bool:
    """В TEST_MODE — только allow-list; иначе все номера."""
    if not TEST_MODE:
        return True
    return phone in ALLOWED_PHONES
