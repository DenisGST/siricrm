"""Уведомления для арбитражного мониторинга (MAX-бот, позже — TG)."""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.maxchat.sender import send_max_message

from . import cooldown
from .models import ArbitrCase

logger = logging.getLogger("arbitr.notify")


def handle_captcha(case: ArbitrCase, *, page_url: str = "") -> None:
    """Реакция на капчу от kad: активировать 12ч-cooldown и (если активировали
    только что) — отправить одиночный алёрт в MAX.

    Повторные капчи во время активного cooldown молчат — флудить смысла нет,
    парсер всё равно остановлен.
    """
    if cooldown.activate():
        send_captcha_alert(case, page_url=page_url)


def send_captcha_alert(case: ArbitrCase, *, page_url: str = "") -> bool:
    """Шлёт в MAX уведомление о капче — чтобы человек зашёл и решил её.

    Возвращает True если отправили; False — если конфиг неполный или ошибка.
    Пока шлём в один общий chat_id (env ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID),
    позже — в персональный MAX сотрудника case.started_by.
    """
    chat_id = (settings.ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID or "").strip()
    token = (settings.MAX_BOT_TOKEN or "").strip()
    if not chat_id or not token:
        logger.warning(
            "Captcha alert skipped: MAX_BOT_TOKEN=%s, ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID=%s",
            bool(token), bool(chat_id),
        )
        return False

    started_by = str(case.started_by) if case.started_by else "—"
    client = case.service.client
    fio = " ".join(filter(None, [client.last_name, client.first_name, client.patronymic]))
    case_number = case.case_number or "(номер не указан)"

    until_dt = cooldown.until()
    if until_dt:
        msk = timezone.localtime(until_dt)
        resume_line = (
            "⏸ Мониторинг приостановлен на 12 часов.\n"
            f"Возобновится: {msk:%d.%m %H:%M} (МСК)\n"
        )
    else:
        resume_line = ""

    text = (
        "⚠️ kad.arbitr.ru показал капчу\n"
        f"{resume_line}"
        f"Первое сорвавшееся дело: {case_number}\n"
        f"Клиент: {fio}\n"
        f"Запустил мониторинг: {started_by}\n"
        f"Открыть kad: {page_url or case.kad_url or 'https://kad.arbitr.ru'}\n"
        "Если решишь капчу раньше — на сервере: "
        "`python manage.py arbitr_clear_cooldown`"
    )

    ok, msg_id, err = send_max_message(
        access_token=token, chat_id=chat_id, text=text,
    )
    if not ok:
        logger.error("Captcha alert MAX send failed: %s", err)
        return False
    logger.info("Captcha alert sent to MAX %s (msg_id=%s)", chat_id, msg_id)
    return True
