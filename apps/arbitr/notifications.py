"""Уведомления для арбитражного мониторинга (MAX-бот + Telegram-бот)."""
from __future__ import annotations

import html
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


class _MarkdownFmt:
    """Разметка MAX (markdown). Скобки [] внутри подписи ссылки рвут разметку."""

    @staticmethod
    def esc(s: str) -> str:
        return s or ""

    @staticmethod
    def link(text: str, url: str) -> str:
        return f"[{_md_link_text(text)}]({url})"


class _HtmlFmt:
    """Разметка Telegram (parse_mode=HTML) — экранируем < > &."""

    @staticmethod
    def esc(s: str) -> str:
        return html.escape(s or "", quote=False)

    @staticmethod
    def link(text: str, url: str) -> str:
        return f'<a href="{html.escape(url or "", quote=True)}">{html.escape(text or "", quote=False)}</a>'


def _build_court_event_text(
    case: ArbitrCase, new_events_detail: list, fmt=_MarkdownFmt,
) -> str:
    """Собирает текст персонального уведомления.

    Формат (одинаков для обоих каналов, отличается только разметка ссылок —
    `fmt` = _MarkdownFmt для MAX / _HtmlFmt для Telegram):
        [A12-1234/2025](kad-карточка) Фамилия Имя Отчество
        Новая запись: Отзыв
        Новый файл: [Определение](kad-ссылка-на-файл)
    Номер дела — ссылка на карточку kad; каждый файл — ссылка на файл на kad.
    """
    fio = _case_fio(case)
    case_number = case.case_number or "(номер не указан)"
    if case.kad_url:
        header = f"{fmt.link(case_number, case.kad_url)} {fmt.esc(fio)}"
    else:
        header = fmt.esc(f"{case_number} {fio}")

    lines = [header]
    shown = new_events_detail[:MAX_EVENTS_IN_NOTIFY]
    for ev in shown:
        kind = (ev.get("kind") or "").strip()          # тип акта
        desc = (ev.get("description") or "").strip()    # суть/результат
        date = (ev.get("date") or "").strip()
        # Основная строка — ТИП акта (kind). title (.case-subject = судья/сторона)
        # НЕ используем: kad кладёт туда ФИО судьи, а не описание записи.
        label = kind or desc or "(без описания)"
        date_part = f" ({date})" if date else ""
        lines.append(fmt.esc(f"Новая запись{date_part}: {label}"))
        # Суть/результат — отдельной строкой (если есть и не дублирует тип).
        if desc and desc != label:
            lines.append(fmt.esc(desc))
        for att in ev.get("attachments") or []:
            fname = (att.get("name") or "").strip() or "файл"
            url = (att.get("kad_url") or "").strip()
            if url and not att.get("is_locked"):
                lines.append(f"Новый файл: {fmt.link(fname, url)}")
            else:
                lines.append(fmt.esc(f"Новый файл: {fname}"))

    hidden = len(new_events_detail) - len(shown)
    if hidden > 0:
        lines.append(fmt.esc(f"…и ещё {hidden} нов. записей — см. карточку дела на kad."))

    text = "\n".join(lines)
    if len(text) > MAX_TEXT_LEN:
        # Режем по границе строки (чтобы не порвать ссылку — она всегда
        # целиком внутри одной строки, в любой из двух разметок).
        cut = text.rfind("\n", 0, MAX_TEXT_LEN)
        if cut <= 0:
            cut = MAX_TEXT_LEN
        text = text[:cut] + "\n… (сообщение сокращено, см. карточку дела на kad)"
    return text


def send_court_event_notifications(
    case: ArbitrCase, *, new_events_detail: list,
) -> int:
    """Персональные уведомления сотрудникам о новых судебных событиях по делу.

    Два независимых канала — MAX и Telegram; сотрудник может включить любой,
    оба или ни одного (чеки в профиле + привязанный chat_id соответствующего
    бота). Только НЕПУСТЫЕ (передан хотя бы один new_event) — пустые парсинги
    молчат. Возвращает суммарное число успешных отправок по обоим каналам.
    """
    if not new_events_detail:
        return 0
    return (
        _send_court_events_max(case, new_events_detail)
        + _send_court_events_telegram(case, new_events_detail)
    )


def _send_court_events_max(case: ArbitrCase, new_events_detail: list) -> int:
    """Рассылка в MAX: `notify_court_events_max=True` + привязанный max_chat_id."""
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

    text = _build_court_event_text(case, new_events_detail, _MarkdownFmt)
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
    logger.info("Court-event notify (MAX): case=%s → %s/%s sent",
                case.case_number, sent, len(recipients))
    return sent


def _send_court_events_telegram(case: ArbitrCase, new_events_detail: list) -> int:
    """Рассылка в Telegram: `notify_court_events_telegram=True` + telegram_chat_id.

    🛑 Сотрудник должен один раз написать боту (нажать Start / отправить код
    привязки) — иначе Telegram отвечает «chat not found»: бот не может писать
    первым. Привязка как раз и делается сообщением боту, так что у привязанных
    диалог уже открыт.
    """
    from apps.telegram.bot_sender import bot_token, send_bot_message

    if not bot_token():
        return 0

    from apps.core.models import Employee
    recipients = list(
        Employee.objects
        .filter(notify_court_events_telegram=True, is_active=True)
        .exclude(telegram_chat_id__isnull=True)
        .values_list("telegram_chat_id", flat=True)
    )
    if not recipients:
        return 0

    text = _build_court_event_text(case, new_events_detail, _HtmlFmt)
    sent = 0
    for chat_id in recipients:
        try:
            ok, _mid, err = send_bot_message(chat_id, text, parse_mode="HTML")
        except Exception:  # noqa: BLE001 — уведомление не должно ронять парсинг
            logger.exception("Court-event TG send crashed for %s", chat_id)
            continue
        if ok:
            sent += 1
        else:
            logger.error("Court-event TG send failed to %s: %s", chat_id, err)
    logger.info("Court-event notify (TG): case=%s → %s/%s sent",
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
