"""Уведомления для арбитражного мониторинга (MAX-бот, позже — TG)."""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.maxchat.sender import send_max_message

from . import cooldown
from .models import ArbitrCase

logger = logging.getLogger("arbitr.notify")


# Сколько новых записей максимум перечислять в одном уведомлении (первый парсинг
# только что подтверждённого дела делает «новыми» ВСЕ события — иначе гигант).
MAX_EVENTS_IN_NOTIFY = 12
# MAX Bot API режет сообщения длиннее 4000 символов — держим запас.
MAX_TEXT_LEN = 3800


def _md_link_text(s: str) -> str:
    """Текст ВНУТРИ [..] markdown-ссылки: скобки [] рвут разметку MAX
    (реальные имена файлов kad бывают вида «[Подписано] …»). Меняем на ()."""
    return (s or "").replace("[", "(").replace("]", ")")


def _case_fio(case: ArbitrCase) -> str:
    client = case.service.client if case.service else None
    fio = " ".join(filter(None, [
        client.last_name if client else "",
        client.first_name if client else "",
        client.patronymic if client else "",
    ])).strip()
    return fio or "(без ФИО)"


def _build_court_event_text(case: ArbitrCase, new_events_detail: list) -> str:
    """Собирает текст персонального уведомления (MAX markdown).

    Формат:
        [A12-1234/2025](kad-карточка) Фамилия Имя Отчество
        Новая запись: Отзыв
        Новый файл: [Определение](kad-ссылка-на-файл)
    Номер дела — ссылка на карточку kad; каждый файл — ссылка на файл на kad.
    """
    fio = _case_fio(case)
    case_number = case.case_number or "(номер не указан)"
    if case.kad_url:
        header = f"[{_md_link_text(case_number)}]({case.kad_url}) {fio}"
    else:
        header = f"{case_number} {fio}"

    lines = [header]
    shown = new_events_detail[:MAX_EVENTS_IN_NOTIFY]
    for ev in shown:
        name = (ev.get("title") or ev.get("kind") or "").strip() or "(без названия)"
        lines.append(f"Новая запись: {name}")
        for att in ev.get("attachments") or []:
            fname = (att.get("name") or "").strip() or "файл"
            url = (att.get("kad_url") or "").strip()
            if url and not att.get("is_locked"):
                lines.append(f"Новый файл: [{_md_link_text(fname)}]({url})")
            else:
                lines.append(f"Новый файл: {fname}")

    hidden = len(new_events_detail) - len(shown)
    if hidden > 0:
        lines.append(f"…и ещё {hidden} нов. записей — см. карточку дела на kad.")

    text = "\n".join(lines)
    if len(text) > MAX_TEXT_LEN:
        # Режем по границе строки (чтобы не порвать markdown-ссылку [..](..)).
        cut = text.rfind("\n", 0, MAX_TEXT_LEN)
        if cut <= 0:
            cut = MAX_TEXT_LEN
        text = text[:cut] + "\n… (сообщение сокращено, см. карточку дела на kad)"
    return text


def send_court_event_notifications(
    case: ArbitrCase, *, new_events_detail: list,
) -> int:
    """Персональные MAX-уведомления сотрудникам о новых судебных событиях по делу.

    Шлём каждому сотруднику с `notify_court_events_max=True` и привязанным
    `max_chat_id`. Только НЕПУСТЫЕ (передан хотя бы один new_event) — пустые
    парсинги молчат. Возвращает число успешных отправок.
    """
    if not new_events_detail:
        return 0
    token = (settings.MAX_BOT_TOKEN or "").strip()
    if not token:
        return 0

    from apps.core.models import Employee
    recipients = list(
        Employee.objects
        .filter(notify_court_events_max=True, is_active=True)
        .exclude(max_chat_id__isnull=True)
        .exclude(max_chat_id="")
        .values_list("max_chat_id", flat=True)
    )
    if not recipients:
        return 0

    text = _build_court_event_text(case, new_events_detail)
    sent = 0
    for chat_id in recipients:
        try:
            ok, _mid, err = send_max_message(
                access_token=token, chat_id=str(chat_id), text=text,
                text_format="markdown",
            )
        except Exception:  # noqa: BLE001 — уведомление не должно ронять парсинг
            logger.exception("Court-event MAX send crashed for %s", chat_id)
            continue
        if ok:
            sent += 1
        else:
            logger.error("Court-event MAX send failed to %s: %s", chat_id, err)
    logger.info("Court-event notify: case=%s → %s/%s sent",
                case.case_number, sent, len(recipients))
    return sent


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
