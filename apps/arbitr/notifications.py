"""Уведомления для арбитражного мониторинга (MAX-бот, позже — TG)."""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.maxchat.sender import send_max_message

from . import cooldown
from .models import ArbitrCase

logger = logging.getLogger("arbitr.notify")


def send_parsed_alert(
    case: ArbitrCase, *,
    new_events: int,
    new_files: int,
    duration_sec: int,
) -> bool:
    """После успешного парсинга шлёт в MAX короткую сводку:
    «А12-…/2025 — Иванов И. И. · скачано 3 новых записей, 1 новый файл · 67с»
    """
    chat_id = (settings.ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID or "").strip()
    token = (settings.MAX_BOT_TOKEN or "").strip()
    if not chat_id or not token:
        return False

    client = case.service.client if case.service else None
    fio = (
        " ".join(filter(None, [
            client.last_name if client else "",
            client.first_name if client else "",
            client.patronymic if client else "",
        ])).strip() or "(без ФИО)"
    )
    case_number = case.case_number or "(номер не указан)"
    text = (
        f"✅ {case_number} · {fio}\n"
        f"Скачано: {new_events} нов. записей, {new_files} нов. файл(ов) · {duration_sec}с"
    )
    ok, _msg_id, err = send_max_message(
        access_token=token, chat_id=chat_id, text=text,
    )
    if not ok:
        logger.error("Parsed alert MAX send failed: %s", err)
    return ok


def handle_captcha(case: ArbitrCase, *, page_url: str = "", ip: str = "") -> None:
    """Реакция на капчу от kad: активировать 12ч-cooldown для ЭТОГО outbound IP
    и (если активировали только что) — отправить одиночный алёрт в MAX.

    Повторные капчи на том же IP во время активного cooldown молчат —
    флудить смысла нет, runner'ы на этом IP всё равно остановлены.
    Другие IP продолжают парсить как обычно.
    """
    if cooldown.activate(ip):
        send_captcha_alert(case, page_url=page_url, ip=ip)


def send_captcha_alert(case: ArbitrCase, *, page_url: str = "", ip: str = "") -> bool:
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

    ip_label = ip or "неизвестный"
    until_dt = cooldown.until(ip)
    if until_dt:
        msk = timezone.localtime(until_dt)
        resume_line = (
            f"⏸ IP {ip_label} приостановлен на 12 часов.\n"
            f"Возобновится автоматически: {msk:%d.%m %H:%M} (МСК)\n"
        )
    else:
        resume_line = ""

    text = (
        f"⚠️ kad.arbitr.ru показал капчу на IP {ip_label}\n"
        f"{resume_line}"
        f"Первое сорвавшееся дело: {case_number}\n"
        f"Клиент: {fio}\n"
        f"Запустил мониторинг: {started_by}\n\n"
        "ℹ️ Парсинг через другие IP продолжается. Этот IP сам "
        "разблокируется через 12ч."
    )

    ok, msg_id, err = send_max_message(
        access_token=token, chat_id=chat_id, text=text,
    )
    if not ok:
        logger.error("Captcha alert MAX send failed: %s", err)
        return False
    logger.info("Captcha alert sent to MAX %s (msg_id=%s)", chat_id, msg_id)
    return True
