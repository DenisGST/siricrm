"""Celery-задачи для интеграции с Telegram Bot API.

`poll_telegram_leads` дёргается из Celery beat каждые ~10 секунд:
вызывает getUpdates с long-polling timeout=20, прогоняет каждое
сообщение через парсер `_parse_lead` и создаёт лиды в CRM.

Используется как альтернатива webhook'у — там, где Telegram не
может достучаться до сервера (наш случай: split-tunnel WireGuard
заворачивает ответный SYN-ACK обратно в туннель).
"""
import logging

import requests
from celery import shared_task
from django.core.cache import cache

from .leads_bot import (
    BOT_TOKEN, LEADS_CHANNEL_ID, _parse_lead, create_lead_from_parsed,
)

logger = logging.getLogger("telegram_leads")

OFFSET_CACHE_KEY = "telegram_leads:update_offset"
POLL_LOCK_KEY = "telegram_leads:poll_lock"
POLL_TIMEOUT = 20  # секунд long-polling


@shared_task(name="telegram.poll_telegram_leads", time_limit=POLL_TIMEOUT + 15)
def poll_telegram_leads():
    """Один цикл getUpdates → парс → создание лидов. Возвращает кол-во
    созданных/повторных лидов. Параллельные вызовы getUpdates Telegram
    отбивает 409 Conflict — поэтому держим лок на время цикла."""
    if not BOT_TOKEN:
        return {"skipped": "no_bot_token"}

    # SETNX-лок через cache.add — отсекает соседние beat-тики, пока long
    # polling висит. Таймаут с запасом, чтобы лок снялся, даже если воркер упал.
    if not cache.add(POLL_LOCK_KEY, "1", timeout=POLL_TIMEOUT + 10):
        return {"skipped": "locked"}

    try:
        return _poll_once()
    finally:
        cache.delete(POLL_LOCK_KEY)


@shared_task(name="telegram.poll_bot_private", time_limit=POLL_TIMEOUT + 15)
def poll_bot_private():
    """Тот же getUpdates, но обрабатываем ТОЛЬКО личку бота: коды привязки
    аккаунта сотрудника (персональные уведомления о судебных событиях).

    Отдельная задача, потому что `poll-telegram-leads` выключен в beat на
    обоих серверах (лид-канал не настроен), а привязка нужна.

    🛑 Одновременно с `poll_telegram_leads` включать НЕЛЬЗЯ: getUpdates на
    один токен из двух мест — 409 Conflict и разъезжающиеся offset'ы.
    Общий SETNX-лок (POLL_LOCK_KEY) страхует от параллельного запуска,
    но offset'ы у задач разные — включаем в beat что-то одно.
    Если понадобятся ОБА (лиды + привязка) — включать только
    `poll-telegram-leads`: его `_poll_once` уже умеет и то, и другое.
    """
    from django.conf import settings

    if not BOT_TOKEN:
        return {"skipped": "no_bot_token"}
    # 🛑 На dev тот же токен поллит бот мониторинга (MONITOR_BOT_POLL=true) —
    # второй getUpdates даёт 409 Conflict. Там коды привязки ловит он сам
    # (apps/core/tasks._handle_monitor_update), а мы уступаем.
    if getattr(settings, "MONITOR_BOT_POLL", False):
        return {"skipped": "monitor_bot_owns_polling"}
    if not cache.add(POLL_LOCK_KEY, "1", timeout=POLL_TIMEOUT + 10):
        return {"skipped": "locked"}
    try:
        return _poll_once(private_only=True)
    finally:
        cache.delete(POLL_LOCK_KEY)


def _poll_once(private_only: bool = False):
    offset = cache.get(OFFSET_CACHE_KEY)
    params = {
        "timeout": POLL_TIMEOUT,
        "allowed_updates": '["channel_post","edited_channel_post","message"]',
    }
    if offset is not None:
        params["offset"] = offset

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params=params, timeout=POLL_TIMEOUT + 10)
    except requests.RequestException as e:
        logger.warning("telegram-leads polling: запрос упал: %s", e)
        return {"error": str(e)}

    if r.status_code != 200:
        logger.warning("telegram-leads polling: %s %s", r.status_code, r.text[:300])
        return {"error": f"http_{r.status_code}"}

    data = r.json()
    if not data.get("ok"):
        return {"error": data.get("description")}

    updates = data.get("result") or []
    leads = 0
    last_id = None
    for upd in updates:
        last_id = upd.get("update_id")
        msg = (
            upd.get("channel_post")
            or upd.get("edited_channel_post")
            or upd.get("message")
        )
        if not msg:
            continue
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        # Личка бота — это НЕ лид, а сотрудник: /start или код привязки
        # аккаунта для персональных уведомлений (см. _handle_private_message).
        if (chat.get("type") or "") == "private":
            try:
                _handle_private_message(msg)
            except Exception:  # noqa: BLE001 — приватка не должна ронять поллинг
                logger.exception("telegram-bot: ошибка обработки лички")
            continue
        # Режим «только личка» (задача poll_bot_private): всё остальное
        # пролистываем, лиды не создаём.
        if private_only:
            continue
        if LEADS_CHANNEL_ID and chat_id != LEADS_CHANNEL_ID:
            logger.info("telegram-leads polling: пропущен chat_id=%s", chat_id)
            continue
        text = msg.get("text") or msg.get("caption") or ""
        parsed = _parse_lead(text)
        if not parsed:
            continue
        try:
            create_lead_from_parsed(parsed)
            leads += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("telegram-leads polling: ошибка создания лида: %s", e)

    if last_id is not None:
        # offset = last + 1 — Telegram перестанет отдавать обработанные.
        cache.set(OFFSET_CACHE_KEY, last_id + 1, timeout=7 * 24 * 3600)

    return {"updates": len(updates), "leads": leads}


# ─── личка бота: привязка аккаунта сотрудника ──────────────


def _handle_private_message(msg: dict):
    """Приватное сообщение боту: одноразовый код привязки или подсказка.

    Зеркало MAX-флоу (`apps/maxchat/processing.handle_max_event`): если текст —
    активный код из профиля сотрудника, сохраняем его chat_id в
    `Employee.telegram_chat_id`. Клиента из лички НЕ создаём — это сотрудник.
    """
    from .bot_sender import send_bot_message
    from . import linkcode

    chat_id = ((msg.get("chat") or {}).get("id"))
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return

    emp_id = linkcode.claim(text)
    if emp_id is not None:
        _bind_employee_telegram(emp_id, int(chat_id))
        return

    # 🛑 На dev тот же токен параллельно поллит бот мониторинга
    # (MONITOR_BOT_POLL=true), и апдейты между поллерами делятся случайно.
    # Своим он отвечает клавиатурой отчётов — не перебиваем его подсказкой.
    # Код привязки выше обрабатываем в любом случае: монитор его игнорирует.
    from django.conf import settings

    from apps.core.tasks import _monitor_allowed_ids
    if getattr(settings, "MONITOR_BOT_POLL", False) and \
            str(chat_id) in _monitor_allowed_ids():
        return

    # Не код — короткая подсказка (на /start и на любой другой текст).
    send_bot_message(
        chat_id,
        "Это служебный бот SiriCRM.\n"
        "Чтобы получать уведомления о судебных событиях, откройте свой профиль "
        "в CRM → «Уведомления в Telegram» → «Получить код привязки» и отправьте "
        "этот код сюда.",
        parse_mode=None,
    )


def _bind_employee_telegram(emp_id: int, chat_id: int):
    """Привязать Telegram chat_id к профилю сотрудника (по одноразовому коду).

    Снимает этот chat_id с других сотрудников (поле unique), сохраняет и
    шлёт сотруднику подтверждение в Telegram.
    """
    from apps.core.models import Employee

    from .bot_sender import send_bot_message

    try:
        emp = Employee.objects.select_related("user").get(pk=emp_id)
    except Employee.DoesNotExist:
        logger.warning("TG link: сотрудник %s не найден (код устарел?)", emp_id)
        return

    # chat_id уникален на Employee — снимем его с прежних владельцев (перепривязка).
    Employee.objects.filter(telegram_chat_id=chat_id).exclude(pk=emp.pk).update(
        telegram_chat_id=None)
    emp.telegram_chat_id = chat_id
    emp.save(update_fields=["telegram_chat_id"])
    logger.info("🔗 Telegram привязан к сотруднику %s (chat_id=%s)", emp.pk, chat_id)

    name = emp.user.get_full_name() or emp.user.username
    send_bot_message(
        chat_id,
        f"✅ Telegram привязан к профилю: {name}.\n"
        "Сюда будут приходить уведомления о судебных событиях "
        "(если включён чек «Уведомлять в Telegram о судебных событиях» в профиле).",
        parse_mode=None,
    )
