"""Автонаполнение доски колл-центра.

Два источника кладут карточку на доску:

1. **Входящий звонок с неизвестного номера** — номера нет в базе, значит
   обращение никем не подхвачено. Заводим неидентифицированного клиента
   (как это давно делает WhatsApp для незнакомого отправителя) и ставим его
   карточку в колонку-приёмник.
2. **Лид из Telegram-канала** — заявка с лендинга, которую разбирает
   ``apps.telegram.leads_bot``.

🛑 Куда именно попадёт карточка, решают флаги на колонке
(``catch_unknown_calls`` / ``catch_telegram_leads``), а не код: имя колонки
в коде означало бы, что переименование в панели ломает приём. Флаг заодно
работает выключателем — не отмечен ни у одной колонки, источник молчит.

🛑 Ни одна функция отсюда не имеет права уронить вызывающего: приём звонков
с АТС и поллер лидов важнее доски. Всё, что может упасть, ловится и пишется
в лог — тем же приёмом, что ``telephony.services._sync_missed_register``.
"""
from __future__ import annotations

import logging
import re

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

SOURCE_CALL = "call"
SOURCE_TG_LEAD = "tg_lead"

# Звонок старше этого не создаёт ни клиента, ни карточку. 🛑 Без порога
# бэкфилл CDR (13 тыс. исторических звонков одной пачкой) завёл бы тысячи
# клиентов и завалил доску — тот же предохранитель, что у реестра
# пропущенных (MISSED_REGISTER_MAX_AGE_HOURS).
MAX_CALL_AGE_HOURS = 48


def blacklist_key(raw) -> str:
    """Ключ номера для чёрного списка.

    🛑 Один номер приходит в трёх видах («+7 (900) …», «8900…», «7900…») —
    без общего ключа список ловил бы только ту запись, в которой номер завели.
    Не-российский номер ``normalize_phone`` не осилит: тогда ключом идут
    голые цифры, чтобы такой номер всё же можно было заблокировать вручную.
    """
    from apps.crm.phone_utils import normalize_phone

    return normalize_phone(raw) or re.sub(r"\D", "", str(raw or ""))


def is_blacklisted(phone) -> bool:
    """Номер в чёрном списке? Ошибку проверки трактуем как «не в списке»:
    сломанный список не должен останавливать приём обращений."""
    from .models import BlockedPhone

    key = blacklist_key(phone)
    if not key:
        return False
    try:
        return BlockedPhone.objects.filter(phone=key, is_active=True).exists()
    except Exception:  # noqa: BLE001
        logger.exception("колл-центр: не удалось проверить чёрный список")
        return False


def note_blacklist_hit(phone) -> None:
    """Отметить срабатывание — по счётчику видно, кто реально долбит."""
    from django.db.models import F

    from .models import BlockedPhone

    key = blacklist_key(phone)
    if not key:
        return
    try:
        (BlockedPhone.objects.filter(phone=key, is_active=True)
         .update(hits=F("hits") + 1, last_seen_at=timezone.now()))
    except Exception:  # noqa: BLE001
        logger.debug("колл-центр: счётчик чёрного списка не обновлён", exc_info=True)


def block_phone(raw, *, comment: str = "", employee=None):
    """Внести номер в чёрный список. → (BlockedPhone | None, created).

    Повторное внесение не плодит записей и оживляет отключённую запись —
    оператор нажал «спам» ещё раз, значит номер снова звонит.
    """
    from .models import BlockedPhone

    key = blacklist_key(raw)
    if not key:
        return None, False
    obj, created = BlockedPhone.objects.get_or_create(
        phone=key,
        defaults={"comment": comment[:255], "added_by": employee},
    )
    if not created and not obj.is_active:
        obj.is_active = True
        if comment:
            obj.comment = comment[:255]
        obj.save(update_fields=["is_active", "comment", "updated_at"])
    return obj, created


def _column_for(source: str):
    """Колонка-приёмник источника. None — источник выключен."""
    from .models import CallCenterColumn

    field = next((f for f, key in CallCenterColumn.CATCH_FLAGS.items() if key == source), None)
    if field is None:
        return None
    return CallCenterColumn.objects.filter(**{field: True}, is_active=True).first()


def place_on_board(client, *, source: str, comment: str = ""):
    """Поставить клиента на доску. → (CallCenterCard | None, created).

    Идемпотентно: у клиента одна карточка. Уже стоит — не трогаем ни
    колонку, ни момент перемещения (оператор мог увести её дальше по доске,
    и повторный звонок не должен откатывать её в «Новые»).
    """
    from .models import CallCenterCard

    if client is None:
        return None, False
    column = _column_for(source)
    if column is None:
        return None, False

    card, created = CallCenterCard.objects.get_or_create(
        client=client,
        defaults={"column": column, "source": source},
    )
    if created:
        logger.info("колл-центр: карточка %s → «%s» (%s)", client.pk, column.name, source)
    return card, created


# ── источник 1: входящий звонок с неизвестного номера ───────────────────────

# «"Петров Пётр" <89005550022>» → «Петров Пётр». 🛑 Свой разбор, а не
# telephony.parse_clid: тот заточен под ВНУТРЕННИЕ номера (в шаблоне жёстко
# три цифры в <>) и на внешнем номере отдаёт пустое имя.
_CLID_NAME_RE = re.compile(r'^"?(?P<name>[^"<]*)"?\s*(<[^>]*>)?\s*$')


def _caller_name(raw: str) -> str:
    """Отображаемое имя из CallerID. Номер вместо имени → пусто."""
    value = (raw or "").strip()
    m = _CLID_NAME_RE.match(value)
    name = (m.group("name") if m else value).strip().strip('"').strip()
    if not name:
        return ""
    # Транки часто кладут в имя сам номер — «Клиент 79537741564» бесполезен.
    if not re.sub(r"[\d\s+()\-]", "", name):
        return ""
    return name


def _client_name_for_call(phone: str) -> tuple[str, str]:
    """Имя автосозданного клиента: «Звонок» + хвост номера и дата.

    Без хвоста и даты десятки автолидов в списке неразличимы — приём тот же,
    что у WhatsApp (``apps.whatsapp.processing``).
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    return "Звонок", f"{digits[-4:]} {timezone.localtime().strftime('%d.%m')}"


def ensure_client_for_unknown_phone(phone: str, *, clid_name: str = ""):
    """Найти клиента по номеру, а если его нет — завести. → (Client | None, created).

    🛑 Гонка реальна: параллельный обзвон 201 и 202 даёт два ``DialBegin`` по
    одному звонку почти одновременно. Защита двухслойная — проверка внутри
    транзакции и уникальность ``ClientPhone(phone, purpose)``: проигравший
    забег откатывает своего клиента и берёт чужого.
    """
    from apps.crm.models import Client
    from apps.crm.phone_utils import (add_client_phone, find_client_by_phone,
                                      format_phone, normalize_phone)

    normalized = normalize_phone(phone) or ""
    if not normalized:
        return None, False

    existing = find_client_by_phone(normalized)
    if existing is not None:
        return existing, False

    first, last = _client_name_for_call(normalized)
    caller = _caller_name(clid_name)
    if caller:
        # Имя от транка есть — раскладываем как WhatsApp по профилю: первое
        # слово в имя, остаток в фамилию.
        parts = caller.split(maxsplit=1)
        first, last = parts[0][:255], (parts[1][:255] if len(parts) > 1 else "")

    with transaction.atomic():
        again = find_client_by_phone(normalized)
        if again is not None:
            return again, False
        client = Client.objects.create(
            first_name=first,
            last_name=last,
            phone=format_phone(normalized),
            status="lead",
            is_identified=False,
        )
        cp = add_client_phone(client, normalized, "primary")
        # 🛑 Проверяем ВЛАДЕЛЬЦА, а не просто «не None»: при конфликте
        # уникальности add_client_phone отдаёт запись победителя гонки —
        # чужую. Без этой проверки проигравшие ноги звонка оставляли бы
        # клиентов-сирот без единого телефона.
        if cp is None or cp.client_id != client.pk:
            client.delete()
            return find_client_by_phone(normalized), False
    return client, True


def handle_unknown_incoming_call(phone: str, *, clid_name: str = "", call=None,
                                 started_at=None):
    """Входящий с незнакомого номера → клиент + карточка. → Client | None.

    Возвращает клиента (в т.ч. уже существовавшего), чтобы вызывающий мог
    подставить его в свою запись — карточке «вам звонили» и в ``Call.client``.
    """
    from apps.crm import client_log
    from apps.crm.phone_utils import find_client_by_phone, normalize_phone

    try:
        normalized = normalize_phone(phone) or ""
        if not normalized:
            return None

        # Порог возраста — предохранитель от бэкфилла (см. MAX_CALL_AGE_HOURS).
        when = started_at or (getattr(call, "started_at", None))
        if when is not None and (timezone.now() - when).total_seconds() > MAX_CALL_AGE_HOURS * 3600:
            return None

        # 🛑 Чёрный список проверяем ПЕРВЫМ и до создания клиента — весь
        # смысл списка в том, чтобы робот-обзвон не оседал в базе лидами.
        if is_blacklisted(normalized):
            note_blacklist_hit(normalized)
            logger.info("колл-центр: звонок с %s пропущен — номер в чёрном списке",
                        normalized)
            return None

        # Номер известен — обращение уже в чьей-то работе, доска ни при чём.
        if find_client_by_phone(normalized) is not None:
            return None

        # Источник выключен (нет колонки-приёмника) — клиента тоже НЕ заводим:
        # иначе выключенная доска молча плодила бы лиды.
        if _column_for(SOURCE_CALL) is None:
            return None

        client, created = ensure_client_for_unknown_phone(normalized, clid_name=clid_name)
        if client is None:
            return None

        card, card_created = place_on_board(client, source=SOURCE_CALL)
        if created:
            from apps.crm.lead_routing import _system_bot_employee
            client_log.record_event(
                client, "incoming_call", employee=_system_bot_employee(),
                comment=(f"Входящий звонок с неизвестного номера {client.phone}. "
                         f"Заведена карточка на доске колл-центра"
                         + (f" — колонка «{card.column.name}»." if card else ".")),
            )
        return client
    except Exception:  # noqa: BLE001 — доска не должна ронять приём звонков
        logger.exception("колл-центр: не удалось обработать звонок с %s", phone)
        return None


# ── источник 2: лид из Telegram-канала ──────────────────────────────────────

def handle_telegram_lead(client, *, source_label: str = "", repeat: bool = False):
    """Лид с лендинга (канал Telegram) → карточка на доске."""
    try:
        # Номер в чёрном списке — заявка с него на доску не идёт. Клиент при
        # этом уже создан поллером лидов и распределён как обычно: мы
        # управляем только доской, чужой поток не режем.
        phone = getattr(client, "phone", "") or ""
        if phone and is_blacklisted(phone):
            note_blacklist_hit(phone)
            logger.info("колл-центр: лид %s не на доске — номер в чёрном списке",
                        client.pk)
            return None

        card, created = place_on_board(client, source=SOURCE_TG_LEAD)
        if created and card is not None:
            logger.info("колл-центр: лид %s на доску (%s)",
                        client.pk, source_label or "Telegram")
        return card
    except Exception:  # noqa: BLE001 — доска не должна ронять поллер лидов
        logger.exception("колл-центр: не удалось поставить лид %s на доску",
                         getattr(client, "pk", "?"))
        return None
