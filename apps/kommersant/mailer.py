"""Почтовый транспорт заявок в «Коммерсантъ»: отправка (SMTP) и приём счёта (IMAP).

Почему собственный транспорт, а не django.core.mail: письма уходят НЕ с системного
ящика проекта, а с личного ящика арбитражного управляющего — заявку подаёт он, и
счёт ИД присылает ответом ему же. Креды АУ вводит в своём профиле (e-mail + пароль,
пароль под Fernet), у каждого свои — глобальный EMAIL_BACKEND не подходит. Транспорт
работает с `mail_accounts.MailAccount` (уже с расшифрованным паролем и хостами).

Регламент ИД (bankruptcy.kommersant.ru/index.php?publemail) — в письме должны быть:
  • заполненная заявка с подписью арбитражного управляющего (у нас — PDF с факсимиле);
  • заявка в формате Word;
  • подтверждающие документы (судебный акт о введении процедуры, полномочия а/у).

🛑 Сетевые операции (SMTP/IMAP) вызывать ТОЛЬКО из Celery — в ASGI-обработчике
   они вешают daphne (инцидент WA-вебхука 09.06.2026). См. tasks.py.
"""
from __future__ import annotations

import imaplib
import logging
import re
import smtplib
import ssl
from datetime import timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime

from django.utils import timezone

from apps.files.s3_utils import download_file_from_s3

log = logging.getLogger(__name__)

# Сколько дней назад смотреть в ящике при поиске счёта.
INVOICE_LOOKBACK_DAYS = 30
# Таймаут сетевых операций, сек.
TIMEOUT = 60


class KommersantMailError(RuntimeError):
    pass


# ── общее ───────────────────────────────────────────────────────────────────

def _require_account(account, *, need_imap: bool = False):
    if account is None:
        raise KommersantMailError(
            "У арбитражного управляющего не настроена почта для «Коммерсанта». "
            "Профиль сотрудника-АУ → «Почта для публикаций в «Коммерсантъ»."
        )
    if not account.user or not account.password or not account.smtp_host:
        raise KommersantMailError(
            f"У АУ {account.label} не заполнены почтовые реквизиты для «Коммерсанта»."
        )
    if need_imap and not account.imap_host:
        raise KommersantMailError(
            f"У АУ {account.label} не удалось определить IMAP-сервер — счёт не получить."
        )


def _decode(value) -> str:
    """MIME-заголовок → нормальная строка (кириллица в теме/именах файлов)."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return str(value)


# ── отправка заявки ─────────────────────────────────────────────────────────

def build_subject(publication) -> str:
    client = publication.case.service.client
    fio = " ".join(filter(None, [client.last_name, client.first_name, client.patronymic])).strip()
    arb = getattr(publication.case.service, "arbitr_case", None)
    number = (arb.case_number if arb else "") or ""
    tail = f", дело № {number}" if number else ""
    return f"Заявка на публикацию сведений о банкротстве — {fio}{tail}"


def build_body(publication) -> str:
    client = publication.case.service.client
    fio = " ".join(filter(None, [client.last_name, client.first_name, client.patronymic])).strip()
    proc = publication.procedure
    am = proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None
    return (
        "Здравствуйте!\n\n"
        f"Прошу опубликовать сообщение о банкротстве гражданина {fio}.\n\n"
        "Во вложении:\n"
        "  1. Заявка на публикацию, подписанная арбитражным управляющим (PDF).\n"
        "  2. Та же заявка в формате Word.\n"
        "  3. Подтверждающие документы.\n\n"
        "Прошу выставить счёт на оплату.\n\n"
        "С уважением,\n"
        f"{am.full_fio if am else ''}\n"
        f"{'арбитражный управляющий' if am else ''}\n"
        f"{am.phone if am else ''}\n"
    ).replace("\n\n\n", "\n\n")


def _collect_attachments(publication) -> list[tuple[str, str, bytes]]:
    """[(имя файла, content_type, содержимое)] — заявка + подтверждающие документы."""
    items = []
    for sf in (publication.blank_pdf, publication.blank_docx):
        if sf is None:
            continue
        items.append(sf)
    items.extend(
        att.stored_file for att in publication.attachments.select_related("stored_file").all()
        if att.stored_file_id
    )

    out = []
    for sf in items:
        try:
            data = download_file_from_s3(sf.bucket, sf.key)
        except Exception as exc:  # noqa: BLE001
            # Молча отправить заявку без обязательного вложения нельзя — ИД откажет.
            raise KommersantMailError(
                f"Не удалось получить файл «{sf.filename}» из хранилища: {exc}"
            ) from exc
        out.append((sf.filename, sf.content_type or "application/octet-stream", data))
    return out


def send_request(publication, account, *, to_addr: str, employee=None):
    """Отправить заявку в ИД с ящика АУ. Возвращает публикацию со статусом `sent`.

    Message-ID письма сохраняем — по нему потом ловим ответ со счётом.
    """
    _require_account(account)

    if publication.blank_pdf_id is None or publication.blank_docx_id is None:
        raise KommersantMailError(
            "Сначала сформируйте бланк заявки — ИД принимает только заявку "
            "с подписью (PDF) и её же в формате Word."
        )

    attachments = _collect_attachments(publication)
    sender = account.from_addr
    domain = sender.split("@")[-1] if "@" in sender else "localhost"
    message_id = make_msgid(domain=domain)

    msg = EmailMessage()
    msg["Subject"] = build_subject(publication)
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Message-ID"] = message_id
    msg.set_content(build_body(publication))

    for filename, ctype, data in attachments:
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            data, maintype=maintype or "application", subtype=subtype or "octet-stream",
            filename=filename,
        )

    _smtp_send(account, msg, sender=sender, to_addr=to_addr)

    publication.status = publication.STATUS_SENT
    publication.sent_at = timezone.now()
    publication.sent_to = to_addr
    publication.sent_from = sender
    publication.sent_message_id = message_id
    publication.sent_by = employee
    publication.send_error = ""
    publication.save(update_fields=[
        "status", "sent_at", "sent_to", "sent_from", "sent_message_id",
        "sent_by", "send_error", "updated_at",
    ])
    return publication


def _smtp_send(account, msg: EmailMessage, *, sender: str, to_addr: str):
    try:
        if account.use_ssl:
            client = smtplib.SMTP_SSL(
                account.smtp_host, account.smtp_port, timeout=TIMEOUT,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=TIMEOUT)
            client.starttls(context=ssl.create_default_context())
        with client:
            client.login(account.user, account.password)
            client.send_message(msg, from_addr=sender, to_addrs=[to_addr])
    except smtplib.SMTPAuthenticationError as exc:
        raise KommersantMailError(
            f"Почта отклонила логин/пароль АУ {account.label}. "
            f"У Яндекса и Mail.ru нужен ПАРОЛЬ ПРИЛОЖЕНИЯ, а не обычный пароль. ({exc})"
        ) from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise KommersantMailError(f"Не удалось отправить письмо: {exc}") from exc


# ── приём счёта ─────────────────────────────────────────────────────────────

_INVOICE_NUMBER_RE = re.compile(r"счет\D{0,10}?№?\s*([A-Za-zА-Яа-я0-9\-/]+)", re.I)
_INVOICE_DATE_RE = re.compile(r"от\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})")
_AMOUNT_RE = re.compile(r"(\d[\d\s ]{2,})[.,](\d{2})\s*(?:руб|₽|р\.)", re.I)
_INVOICE_HINT_RE = re.compile(r"счет|счёт|invoice|оплат", re.I)


def _imap_connect(account):
    try:
        if account.use_ssl:
            conn = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, timeout=TIMEOUT)
        else:
            conn = imaplib.IMAP4(account.imap_host, account.imap_port, timeout=TIMEOUT)
            conn.starttls(ssl.create_default_context())
        conn.login(account.user, account.password)
        return conn
    except imaplib.IMAP4.error as exc:
        raise KommersantMailError(
            f"IMAP отклонил логин АУ {account.label} (для Яндекс/Mail.ru нужен пароль "
            f"приложения и включённый IMAP в настройках ящика): {exc}"
        ) from exc
    except (OSError, ssl.SSLError) as exc:
        raise KommersantMailError(f"Не удалось подключиться к IMAP: {exc}") from exc


def _body_text(msg) -> str:
    """Плоский текст письма (для эвристик по номеру/сумме счёта)."""
    parts = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        if part.get_content_subtype() not in ("plain", "html"):
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if part.get_content_subtype() == "html":
            text = re.sub(r"(?s)<[^>]*>", " ", text)
        parts.append(text)
    return "\n".join(parts)


def parse_invoice_meta(subject: str, body: str) -> dict:
    """Достать номер / дату / сумму счёта из письма.

    Эвристика: ИД шлёт счёт вложением, а реквизиты дублирует в теме или теле.
    Ничего не нашли — оставляем пустым, юрист впишет руками; выдумывать номер
    счёта нельзя, по нему идёт назначение платежа.
    """
    haystack = f"{subject}\n{body}"
    out: dict = {}

    m = _INVOICE_NUMBER_RE.search(haystack)
    if m:
        number = m.group(1).strip(" .,;:")
        # Отсекаем ложные срабатывания вида «счет в АО «АЛЬФА-БАНК»».
        if any(ch.isdigit() for ch in number):
            out["number"] = number[:64]

    m = _INVOICE_DATE_RE.search(haystack)
    if m:
        raw = m.group(1).replace("-", ".").replace("/", ".")
        parts = raw.split(".")
        if len(parts) == 3:
            day, month, year = parts
            year = f"20{year}" if len(year) == 2 else year
            try:
                from datetime import date
                out["date"] = date(int(year), int(month), int(day))
            except ValueError:
                pass

    m = _AMOUNT_RE.search(haystack)
    if m:
        whole = re.sub(r"[\s ]", "", m.group(1))
        try:
            from decimal import Decimal
            out["amount"] = Decimal(f"{whole}.{m.group(2)}")
        except Exception:  # noqa: BLE001
            pass
    return out


def _attachment_files(msg) -> list[tuple[str, str, bytes]]:
    """Вложения письма: [(имя, content_type, байты)]. Инлайн-картинки пропускаем."""
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = _decode(part.get_filename())
        if not filename and "attachment" not in disposition:
            continue
        if part.get_content_maintype() == "image" and "inline" in disposition:
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        out.append((filename or "vlozhenie", part.get_content_type(), data))
    return out


def fetch_replies(account, *, message_ids: set[str]) -> list[dict]:
    """Найти в ящике АУ ответы на наши заявки.

    Возвращает [{message_id (наш), reply_message_id, subject, date, body, files}].
    Матчим строго по In-Reply-To/References — тема у ИД может быть какой угодно,
    а привязать чужой счёт к делу хуже, чем не привязать никакого.
    """
    if not message_ids:
        return []

    conn = _imap_connect(account)
    found: list[dict] = []
    try:
        conn.select("INBOX", readonly=True)
        since = (timezone.now() - timedelta(days=INVOICE_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        status, data = conn.search(None, "SINCE", since)
        if status != "OK":
            return []
        uids = (data[0] or b"").split()

        for uid in reversed(uids):  # свежие письма первыми
            status, raw = conn.fetch(uid, "(RFC822)")
            if status != "OK" or not raw or not isinstance(raw[0], tuple):
                continue
            msg = message_from_bytes(raw[0][1])

            refs = f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}"
            ours = next((mid for mid in message_ids if mid and mid in refs), None)
            if not ours:
                continue

            try:
                sent_at = parsedate_to_datetime(msg.get("Date"))
            except Exception:  # noqa: BLE001
                sent_at = None
            found.append({
                "message_id": ours,
                "reply_message_id": (msg.get("Message-ID") or "").strip(),
                "subject": _decode(msg.get("Subject")),
                "date": sent_at,
                "body": _body_text(msg),
                "files": _attachment_files(msg),
            })
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return found


def looks_like_invoice(reply: dict) -> bool:
    """Ответ ИД похож на счёт? (бывают автоответы «ваше письмо получено»)."""
    if any(
        _INVOICE_HINT_RE.search(name) for name, _ct, _data in reply.get("files", [])
    ):
        return True
    if reply.get("files") and _INVOICE_HINT_RE.search(reply.get("subject") or ""):
        return True
    return bool(reply.get("files")) and bool(_INVOICE_HINT_RE.search(reply.get("body") or ""))
