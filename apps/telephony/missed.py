"""Пропущенные входящие: регистрация, уведомления, автозакрытие.

Заменяет почтовые уведомления с АТС. Почему заменяет, а не дополняет: письма
с АТС не уходят вообще — postfix релеит через smtp.mail.ru, а тот с некоторых
пор отвечает «535 Net dostupa na vashem tarife» на любую попытку авторизации.
За месяц наблюдений — десятки тысяч отказов и ни одной успешной внешней
доставки, то есть обращения терялись молча (и опечатка в адресе РОПа была
лишь вторым слоем той же беды).

Запись создают ДВА источника (см. ``MissedCall``): диалплан по горячим следам
и выгрузка CDR агентом. Оба зовут ``register`` — она идемпотентна по
``linkedid``.
"""
from __future__ import annotations

import html
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.crm.phone_utils import format_phone

from .models import CallGroup, MissedCall

logger = logging.getLogger(__name__)

# Уведомление о звонке, случившемся давно, только раздражает: человек уже
# ничего не успеет, а в реестре запись всё равно будет. Порог с запасом на
# случай, если CRM была недоступна и агент дослал звонки пачкой.
NOTIFY_MAX_AGE_MINUTES = 60
# Голосовое короче этого — не сообщение, а сброшенная трубка на приветствии
# (у такого файла на АТС 44 байта, пустой заголовок WAV).
MIN_VOICEMAIL_SECONDS = 4


# ── определение группы ──────────────────────────────────────────────────────

def group_by_code(code: str):
    if not code:
        return None
    return CallGroup.objects.filter(code=(code or "").strip().lower(), is_active=True).first()


def group_by_extension(extension: str):
    """Группа по внутреннему номеру — резервный путь для звонков без хука.

    🛑 Перебираем в питоне, а не ищем в SQL: ``extensions`` это строка
    «201,202», и ``LIKE '%201%'`` поймал бы заодно «1201» и «2012».
    """
    ext = (extension or "").strip()
    if not ext:
        return None
    for group in CallGroup.objects.filter(is_active=True):
        if ext in group.extension_list:
            return group
    return None


def resolve_group(*, code: str = "", extension: str = "", dcontext: str = ""):
    """Куда шёл звонок. Приоритет — сигналу диалплана, он точный."""
    group = group_by_code(code)
    if group is not None:
        return group
    group = group_by_extension(extension)
    if group is not None:
        return group
    # Контексты обзвона именуются по отделу: incallcc_b / mess_rec_osd / yuro_b.
    ctx = (dcontext or "").lower()
    for candidate, marker in (("osd", "osd"), ("yuro", "yuro"), ("cc", "cc")):
        if marker in ctx:
            return group_by_code(candidate)
    return None


# ── регистрация ─────────────────────────────────────────────────────────────

def register(
    *, linkedid: str, occurred_at=None, phone: str = "", uniqueid: str = "",
    kind: str = MissedCall.KIND_MISSED, group_code: str = "", extension: str = "",
    dcontext: str = "", voicemail_file: str = "", voicemail_seconds: int = 0,
    call=None, notify: bool = True,
):
    """Создать (или дополнить) запись о пропущенном. → (MissedCall, created).

    Идемпотентно по ``linkedid``. Повторный приход того же звонка из второго
    источника не создаёт дубль и не шлёт второе уведомление — он лишь
    дозаполняет то, чего не знал первый (клиента, ссылку на звонок в журнале,
    голосовое сообщение).
    """
    from .services import normalize_counterparty, resolve_client

    linkedid = (linkedid or "").strip()[:32]
    if not linkedid:
        raise ValueError("linkedid обязателен")

    normalized = normalize_counterparty(phone)
    occurred_at = occurred_at or timezone.now()
    if voicemail_seconds and voicemail_seconds < MIN_VOICEMAIL_SECONDS:
        # Трубку бросили на приветствии — сообщения нет, это обычный пропущенный.
        kind = MissedCall.KIND_MISSED
        voicemail_file, voicemail_seconds = "", 0

    with transaction.atomic():
        existing = MissedCall.objects.select_for_update().filter(linkedid=linkedid).first()
        if existing is None:
            missed = MissedCall.objects.create(
                linkedid=linkedid,
                uniqueid=(uniqueid or "")[:32],
                occurred_at=occurred_at,
                kind=kind,
                group=resolve_group(code=group_code, extension=extension, dcontext=dcontext),
                extension=(extension or "")[:8],
                phone=normalized[:32],
                raw_phone=(phone or "")[:40],
                client=resolve_client(normalized) if normalized else None,
                voicemail_file=(voicemail_file or "")[:255],
                voicemail_seconds=voicemail_seconds or 0,
                call=call,
            )
            created = True
        else:
            missed, created = existing, False
            changed = []
            # Голосовое сообщение важнее «просто пропущен»: второй источник
            # мог узнать про него позже, чем первый создал запись.
            if kind == MissedCall.KIND_VOICEMAIL and missed.kind != MissedCall.KIND_VOICEMAIL:
                missed.kind, changed = MissedCall.KIND_VOICEMAIL, changed + ["kind"]
            for field, value in (
                ("uniqueid", (uniqueid or "")[:32]),
                ("extension", (extension or "")[:8]),
                ("phone", normalized[:32]),
                ("raw_phone", (phone or "")[:40]),
                ("voicemail_file", (voicemail_file or "")[:255]),
            ):
                if value and not getattr(missed, field):
                    setattr(missed, field, value)
                    changed.append(field)
            if voicemail_seconds and not missed.voicemail_seconds:
                missed.voicemail_seconds, changed = voicemail_seconds, changed + ["voicemail_seconds"]
            if call is not None and missed.call_id is None:
                missed.call, changed = call, changed + ["call"]
            if missed.group is None:
                group = resolve_group(code=group_code, extension=extension, dcontext=dcontext)
                if group is not None:
                    missed.group, changed = group, changed + ["group"]
            if missed.client_id is None and missed.phone:
                client = resolve_client(missed.phone)
                if client is not None:
                    missed.client, changed = client, changed + ["client"]
            if changed:
                missed.save(update_fields=list(set(changed)) + ["updated_at"])

    if notify and missed.notified_at is None:
        notify_missed(missed)
    return missed, created


def ensure_from_call(call):
    """Страховка со стороны CDR: звонок без ответа → запись в реестре.

    🛑 Нужна не только на случай, если АТС не достучалась до CRM: через хук
    диалплана вообще не проходят звонки, брошенные в голосовом меню (их
    обрывает Read, а не Dial), а это заметная доля обращений.
    """
    from .models import Call

    if call is None or call.direction != Call.DIRECTION_IN:
        return None
    if call.outcome in (Call.OUTCOME_ANSWERED, ""):
        return None
    if not (call.linkedid or call.uniqueid):
        return None

    kind = MissedCall.KIND_MISSED
    if call.outcome == Call.OUTCOME_VOICEMAIL:
        kind = MissedCall.KIND_VOICEMAIL
    elif not call.extension and (call.dcontext or "").lower().startswith("inbound"):
        kind = MissedCall.KIND_IVR

    missed, _created = register(
        linkedid=call.linkedid or call.uniqueid,
        uniqueid=call.uniqueid,
        occurred_at=call.started_at,
        phone=call.counterparty_phone or call.src,
        kind=kind,
        extension=call.extension,
        dcontext=call.dcontext,
        voicemail_seconds=call.billsec if kind == MissedCall.KIND_VOICEMAIL else 0,
        call=call,
    )
    return missed


def close_open_for_phone(phone: str, *, by_call=None, employee=None):
    """Связались — закрыть открытые обращения по этому номеру.

    Зовётся, когда с номером состоялся разговор (входящий, на который ответили,
    или исходящий из CRM). Смысл: реестр должен показывать долг перед клиентом,
    а не историю; иначе после перезвона строка висит «новой» и её закрывают
    руками, чего никто делать не будет.
    """
    from .services import normalize_counterparty

    normalized = normalize_counterparty(phone)
    if not normalized:
        return 0
    rows = MissedCall.objects.filter(
        phone=normalized, status__in=MissedCall.OPEN_STATUSES)
    if by_call is not None:
        rows = rows.filter(occurred_at__lte=by_call.started_at)
    return rows.update(
        status=MissedCall.STATUS_AUTO_DONE,
        handled_at=timezone.now(),
        handled_by=employee or (by_call.employee if by_call is not None else None),
        closed_by_call=by_call,
        updated_at=timezone.now(),
    )


# ── уведомления ─────────────────────────────────────────────────────────────

def recipients(missed: MissedCall):
    """Кому сообщать: отдел группы ∪ подписчики ∪ (по флагу) руководство.

    🛑 Владельца внутреннего номера добавляем всегда, даже если он не в отделе
    группы: звонили лично ему — он и должен узнать первым.
    """
    from apps.core.models import Employee
    from apps.core.permissions import MANAGEMENT_ROLES

    ids: set = set()
    group = missed.group
    if group is not None:
        if group.notify_department and group.department_id:
            ids |= set(Employee.objects.filter(
                department_id=group.department_id, is_active=True,
            ).values_list("pk", flat=True))
        ids |= set(group.subscribers.filter(is_active=True).values_list("pk", flat=True))
        if group.notify_management:
            ids |= set(Employee.objects.filter(
                role__in=MANAGEMENT_ROLES, is_active=True,
            ).values_list("pk", flat=True))
    if missed.extension:
        ids |= set(Employee.objects.filter(
            sip_extension=missed.extension, is_active=True,
        ).values_list("pk", flat=True))

    if not ids:
        # Группа не опознана (звонок мимо известных веток) — обращение всё
        # равно нельзя потерять, поэтому уведомляем руководство.
        ids |= set(Employee.objects.filter(
            role__in=MANAGEMENT_ROLES, is_active=True,
        ).values_list("pk", flat=True))

    return (Employee.objects.filter(pk__in=ids, notify_missed_calls=True)
            .select_related("user", "department"))


class _MarkdownFmt:
    """Разметка MAX. Скобки [] внутри подписи ссылки рвут markdown MAX."""

    @staticmethod
    def esc(s: str) -> str:
        return s or ""

    @staticmethod
    def bold(s: str) -> str:
        return f"**{s}**"

    @staticmethod
    def link(text: str, url: str) -> str:
        return f"[{(text or '').replace('[', '(').replace(']', ')')}]({url})"


class _HtmlFmt:
    """Разметка Telegram (parse_mode=HTML)."""

    @staticmethod
    def esc(s: str) -> str:
        return html.escape(s or "", quote=False)

    @staticmethod
    def bold(s: str) -> str:
        return f"<b>{s}</b>"

    @staticmethod
    def link(text: str, url: str) -> str:
        return (f'<a href="{html.escape(url or "", quote=True)}">'
                f'{html.escape(text or "", quote=False)}</a>')


def _client_title(missed: MissedCall) -> str:
    client = missed.client
    if client is None:
        return "номер в базе не найден"
    fio = " ".join(filter(None, [client.last_name, client.first_name, client.patronymic])).strip()
    return fio or "клиент без ФИО"


def missed_url() -> str:
    """Ссылка на реестр. Дашборд умеет ``?open=`` — раздел откроется сам.

    🛑 Ведём на ``/telephony/?tab=missed``, а не на отдельный адрес: дашборд
    принимает в ``?open=`` только пути из меню (защита от того, чтобы ссылкой
    заставить чужой браузер дёрнуть произвольный эндпоинт), а «Звонки» в меню
    есть — реестр живёт вкладкой внутри них.
    """
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}/?open=%2Ftelephony%2F%3Ftab%3Dmissed" if base else ""


def build_text(missed: MissedCall, fmt=_MarkdownFmt) -> str:
    """Текст личного уведомления. Одинаков для обоих каналов, отличается
    только разметка ссылок."""
    phone_display = format_phone(missed.phone) if missed.phone else (
        missed.raw_phone or "номер не определился")

    head = "🎧 Голосовое сообщение" if missed.kind == MissedCall.KIND_VOICEMAIL \
        else "📞 Пропущенный звонок"
    lines = [fmt.bold(fmt.esc(head))]
    lines.append(fmt.esc(f"{phone_display} — {_client_title(missed)}"))

    where = missed.group.name if missed.group else "направление не определено"
    if missed.extension:
        where = f"{where} (вн. {missed.extension})"
    lines.append(fmt.esc(f"{where} · {timezone.localtime(missed.occurred_at):%d.%m в %H:%M}"))

    if missed.kind == MissedCall.KIND_VOICEMAIL and missed.voicemail_seconds:
        lines.append(fmt.esc(f"Записано сообщение {missed.voicemail_seconds} с — "
                             f"послушать можно в CRM."))
    if missed.kind == MissedCall.KIND_IVR:
        lines.append(fmt.esc("Звонивший положил трубку в голосовом меню."))

    url = missed_url()
    if url:
        lines.append(fmt.link("Открыть «Пропущенные» в CRM", url))
    return "\n".join(lines)


def notify_missed(missed: MissedCall) -> int:
    """Разослать уведомление о пропущенном. → число успешных отправок.

    🛑 Уведомление не должно ронять ни приём с АТС, ни разбор CDR: любая
    ошибка канала глотается в лог. Метка ``notified_at`` ставится в любом
    случае — иначе второй источник, дополнив запись, разослал бы всё заново.
    """
    if missed.status not in MissedCall.OPEN_STATUSES:
        return 0
    age_minutes = (timezone.now() - missed.occurred_at).total_seconds() / 60
    if age_minutes > NOTIFY_MAX_AGE_MINUTES:
        MissedCall.objects.filter(pk=missed.pk).update(notified_at=timezone.now())
        return 0

    people = list(recipients(missed))
    sent = _send_max(missed, people) + _send_telegram(missed, people)
    _push_cards(missed, people)
    _record_client_event(missed)
    MissedCall.objects.filter(pk=missed.pk).update(notified_at=timezone.now())
    logger.info("пропущенный %s: уведомлено %s из %s получателей",
                missed.phone, sent, len(people))
    return sent


def _send_max(missed: MissedCall, people) -> int:
    from apps.maxchat.sender import send_max_message

    token = (getattr(settings, "MAX_BOT_TOKEN", "") or "").strip()
    if not token:
        return 0
    text = build_text(missed, _MarkdownFmt)
    sent = 0
    for emp in people:
        if not emp.max_chat_id:
            continue
        try:
            ok, _mid, err = send_max_message(
                access_token=token, chat_id=str(emp.max_chat_id),
                text=text, text_format="markdown")
        except Exception:  # noqa: BLE001 — канал не должен ронять приём звонка
            logger.exception("пропущенный: сбой отправки в MAX для %s", emp)
            continue
        if ok:
            sent += 1
        else:
            logger.error("пропущенный: MAX отказал (%s): %s", emp, err)
    return sent


def _send_telegram(missed: MissedCall, people) -> int:
    from apps.telegram.bot_sender import bot_token, send_bot_message

    if not bot_token():
        return 0
    text = build_text(missed, _HtmlFmt)
    sent = 0
    for emp in people:
        if not emp.telegram_chat_id:
            continue
        try:
            ok, _mid, err = send_bot_message(emp.telegram_chat_id, text, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            logger.exception("пропущенный: сбой отправки в Telegram для %s", emp)
            continue
        if ok:
            sent += 1
        else:
            logger.error("пропущенный: Telegram отказал (%s): %s", emp, err)
    return sent


def _push_cards(missed: MissedCall, people) -> None:
    """Карточка в интерфейсе тем, кто сейчас в CRM.

    Переиспользуем канал ``IncomingCallAlert``: у звонка на группу всплывашки
    по DialBegin может не быть вовсе (звонок оборвался в меню), а увидеть
    обращение сразу — ровно то, ради чего всё делается.
    """
    from .models import IncomingCallAlert
    from .notifications import register_incoming_call

    phone = missed.phone or missed.raw_phone
    # 🛑 Владелец внутреннего номера уже мог получить карточку по DialBegin
    # (слушатель AMI ставит её в момент звонка) — второй такой же карточкой
    # мы бы дублировали одно и то же обращение. Ключ у карточки другой (нога
    # звонка, а не linkedid), поэтому сверяемся по номеру и дню.
    today = timezone.localdate()
    already = set(
        IncomingCallAlert.objects
        .filter(phone=phone, started_at__date=today, dismissed_at__isnull=True)
        .values_list("employee_id", flat=True)
    ) if phone else set()

    for emp in people:
        if emp.pk in already:
            continue
        try:
            register_incoming_call(
                emp,
                channel_key=f"missed:{missed.linkedid}",
                phone=phone,
                client=missed.client,
                missed=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("пропущенный: не удалось показать карточку %s", emp)


def _record_client_event(missed: MissedCall) -> None:
    """Известный клиент — запись в его событийку.

    Так пропущенный виден и тому, кто ведёт клиента (а не только дежурному
    по группе), и остаётся в истории общения.
    """
    if missed.client_id is None:
        return
    from apps.crm import client_log

    label = ("оставил голосовое сообщение"
             if missed.kind == MissedCall.KIND_VOICEMAIL else "звонил, не дозвонился")
    where = f" ({missed.group.name})" if missed.group else ""
    try:
        client_log.record_event(
            missed.client, "call_missed",
            comment=f"Клиент {label}{where}: {missed.phone or missed.raw_phone}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("пропущенный: не удалось записать событие клиента")
