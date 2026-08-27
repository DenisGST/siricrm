"""Разбор данных CDR: направление звонка, внутренний номер, привязка
к сотруднику и клиенту.

Правила направления намеренно строятся по НОМЕРАМ, а не по именам контекстов
диалплана: контексты на АТС правятся руками (inboundb/incallcc_b/osd_b/yuro_b…)
и могут добавиться новые, а «трёхзначный внутренний против длинного внешнего» —
свойство самой нумерации и не поедет.
"""
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.crm.phone_utils import find_client_by_phone, normalize_phone

from .models import Call

logger = logging.getLogger(__name__)

# АТС живёт по московскому времени, в CDR лежит наивная локальная дата.
PBX_TZ = ZoneInfo("Europe/Moscow")

_EXT_RE = re.compile(r"^\d{3}$")


def is_extension(value: str) -> bool:
    """Внутренний номер АТС — ровно три цифры (200…799)."""
    return bool(_EXT_RE.match((value or "").strip()))


def detect_direction(src: str, dst: str) -> str:
    src, dst = (src or "").strip(), (dst or "").strip()
    src_int, dst_int = is_extension(src), is_extension(dst)
    if src_int and dst_int:
        return Call.DIRECTION_INTERNAL
    if src_int:
        return Call.DIRECTION_OUT
    return Call.DIRECTION_IN


def extract_parties(src: str, dst: str, direction: str):
    """→ (внутренний номер сотрудника, внешний номер абонента).

    Для входящих внешний номер — ``src``; ``dst`` там мусорный: это либо `s`,
    либо `1`, либо вовсе логин SIP-транка (`MPBX_g_626106_...`), потому что
    звонок сперва попадает в контекст обработки, а не на конкретную трубку.
    """
    src, dst = (src or "").strip(), (dst or "").strip()
    if direction == Call.DIRECTION_OUT:
        return src, dst
    if direction == Call.DIRECTION_IN:
        return (dst if is_extension(dst) else ""), src
    return src, ""


def parse_pbx_datetime(raw: str):
    """`2026-08-21 19:00:29` из CDR → aware datetime в таймзоне АТС."""
    if not raw:
        return None
    value = str(raw).strip().replace("T", " ")[:19]
    try:
        naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=PBX_TZ)


CLID_RE = re.compile(r'^"?(?P<name>[^"<]*)"?\s*<(?P<ext>\d{3})>')


def parse_clid(clid: str):
    """`"Дмитриева Анна Анатольевна" <301>` → ("Дмитриева Анна Анатольевна", "301")."""
    m = CLID_RE.match((clid or "").strip())
    if not m:
        return "", ""
    return m.group("name").strip(), m.group("ext")


def _norm_name(s: str) -> str:
    return re.sub(r"[^а-яёa-z]", "", (s or "").lower().replace("ё", "е"))


def match_employee_by_name(name: str):
    """Сотрудник по ФИО из CallerID.

    🛑 На АТС ФИО одной строкой «Фамилия Имя Отчество», в CRM — раздельные
    ``first_name``/``last_name``. Совпадение по одной фамилии принимаем, только
    если такая фамилия в CRM единственная.
    """
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return None
    from apps.core.models import Employee
    candidates = [e for e in Employee.objects.select_related("user").filter(user__isnull=False)
                  if _norm_name(e.user.last_name) == _norm_name(parts[0])]
    if len(parts) > 1:
        exact = [e for e in candidates if _norm_name(e.user.first_name) == _norm_name(parts[1])]
        if len(exact) == 1:
            return exact[0]
    return candidates[0] if len(candidates) == 1 else None


def resolve_employee(extension: str, clid: str = ""):
    """Кто вёл ЭТОТ разговор.

    🛑 Сперва по имени в CallerID самого звонка, и только потом по текущему
    владельцу номера. Внутренние номера переходят от человека к человеку
    (502: Попова до февраля 2026 → Кудинов к августу), и привязка «по текущему
    владельцу» задним числом приписала бы сотруднику чужие разговоры — это
    исказило бы и отчёты, и историю клиента.
    """
    name, _ = parse_clid(clid)
    if name:
        emp = match_employee_by_name(name)
        if emp is not None:
            return emp
    if not extension:
        return None
    from apps.core.models import Employee
    return Employee.objects.filter(sip_extension=extension).first()


def resolve_client(phone: str):
    """Клиент по внешнему номеру. Единый источник — ``find_client_by_phone``
    (crm.ClientPhone), а не кэш ``Client.phone``."""
    if not phone:
        return None
    try:
        return find_client_by_phone(phone)
    except Exception:
        return None


def normalize_counterparty(raw: str) -> str:
    """Внешний номер к единому виду. Мусорные значения (`s`, `1`, логин транка)
    отбрасываем — иначе они попадут в поиск клиента и дадут ложные привязки."""
    value = (raw or "").strip()
    if not value or is_extension(value):
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 5:
        return ""
    try:
        return normalize_phone(value) or ""
    except Exception:
        return ""


def build_call_fields(row: dict) -> dict:
    """Из строки CDR (как её шлёт агент) — готовый набор полей модели Call."""
    src = (row.get("src") or "").strip()
    dst = (row.get("dst") or "").strip()
    direction = detect_direction(src, dst)
    extension, counterparty_raw = extract_parties(src, dst, direction)
    counterparty = normalize_counterparty(counterparty_raw)

    started = parse_pbx_datetime(row.get("calldate")) or timezone.now()
    employee = resolve_employee(extension, row.get("clid") or "")
    client = resolve_client(counterparty)

    return {
        "linkedid": (row.get("linkedid") or "")[:32],
        "started_at": started,
        "direction": direction,
        "src": src[:80],
        "dst": dst[:80],
        "clid": (row.get("clid") or "")[:120],
        "extension": extension[:8],
        "employee": employee,
        "counterparty_phone": counterparty[:32],
        "client": client,
        "duration": int(row.get("duration") or 0),
        "billsec": int(row.get("billsec") or 0),
        "disposition": (row.get("disposition") or "")[:20],
        "dcontext": (row.get("dcontext") or "")[:80],
        "userfield": (row.get("userfield") or "")[:255],
        "source_path": (row.get("rec_name") or "")[:255],
        "has_recording_on_pbx": bool(row.get("has_recording")),
    }


# 🛑 Один звонок = несколько строк CDR («ног»): обзвон 201 → 202 → голосовая
# почта пишется тремя строками с общим uniqueid. У ПОСЛЕДНЕЙ ноги (голосовая
# почта) disposition = ANSWERED, хотя с клиентом никто не разговаривал.
# Поэтому итог звонка считаем по всем ногам, а не берём последнюю.
VOICEMAIL_CONTEXT_PREFIXES = ("mess_rec", "order_ticket")
MISSED_CONTEXT_PREFIXES = ("miss_call",)

# Чем «сильнее» итог, тем он важнее при слиянии ног.
OUTCOME_PRIORITY = {
    Call.OUTCOME_ANSWERED: 60,
    Call.OUTCOME_VOICEMAIL: 50,
    Call.OUTCOME_MISSED: 40,
    Call.OUTCOME_NO_ANSWER: 30,
    Call.OUTCOME_BUSY: 20,
    Call.OUTCOME_FAILED: 10,
    "": 0,
}


def leg_outcome(dcontext: str, disposition: str) -> str:
    """Итог одной ноги звонка.

    🛑 Контекст диалплана проверяем ДО disposition: нога голосовой почты
    помечена ANSWERED, и без этой проверки пропущенный звонок выглядел бы
    обслуженным.
    """
    ctx = (dcontext or "").strip().lower()
    if ctx.startswith(VOICEMAIL_CONTEXT_PREFIXES):
        return Call.OUTCOME_VOICEMAIL
    if ctx.startswith(MISSED_CONTEXT_PREFIXES):
        return Call.OUTCOME_MISSED
    disp = (disposition or "").strip().upper()
    if disp == "ANSWERED":
        return Call.OUTCOME_ANSWERED
    if disp == "NO ANSWER":
        return Call.OUTCOME_NO_ANSWER
    if disp == "BUSY":
        return Call.OUTCOME_BUSY
    return Call.OUTCOME_FAILED


def upsert_call(row: dict) -> tuple:
    """Создать или дополнить звонок по ``uniqueid``. → (Call, created).

    Повторный приход того же ``uniqueid`` — это другая нога звонка, а не
    дубль: сливаем, а не перезаписываем. Берём самое раннее начало, самый
    сильный итог и данные той ноги, которая этот итог дала.
    """
    uniqueid = (row.get("uniqueid") or "").strip()[:32]
    if not uniqueid:
        raise ValueError("uniqueid обязателен")

    fields = build_call_fields(row)
    fields["outcome"] = leg_outcome(fields["dcontext"], fields["disposition"])

    existing = Call.objects.filter(uniqueid=uniqueid).first()
    if existing is None:
        call = Call.objects.create(uniqueid=uniqueid, **fields)
        _sync_missed_register(call)
        _sync_callcenter_board(call)
        _sync_callcenter_result(call)
        return call, True

    merged = dict(fields)
    merged["started_at"] = min(existing.started_at, fields["started_at"])

    new_rank = OUTCOME_PRIORITY.get(fields["outcome"], 0)
    old_rank = OUTCOME_PRIORITY.get(existing.outcome, 0)
    if new_rank < old_rank:
        # Прежняя нога важнее — оставляем её характеристики звонка.
        for key in ("outcome", "disposition", "dcontext", "billsec", "duration", "userfield"):
            merged[key] = getattr(existing, key)
    elif new_rank == old_rank:
        merged["billsec"] = max(existing.billsec, fields["billsec"])
        merged["duration"] = max(existing.duration, fields["duration"])

    # Клиента, сотрудника и внешний номер, раз уже определили, не теряем:
    # у ноги голосовой почты dst = `s`, и номера в ней нет.
    for key in ("client", "employee"):
        if merged.get(key) is None and getattr(existing, f"{key}_id"):
            merged[key] = getattr(existing, key)
    for key in ("counterparty_phone", "extension", "clid", "source_path"):
        if not merged.get(key) and getattr(existing, key):
            merged[key] = getattr(existing, key)
    if existing.has_recording_on_pbx:
        merged["has_recording_on_pbx"] = True

    for key, value in merged.items():
        setattr(existing, key, value)
    existing.save()
    _sync_missed_register(existing)
    return existing, False


def to_dial_format(phone: str) -> str:
    """Номер в том виде, в каком его набирают с трубки на этой АТС.

    🛑 Именно `8XXXXXXXXXX`: в CDR все реальные исходящие записаны так, то есть
    этот формат проверен их диалпланом. Нормализованный `7…` тоже дошёл бы
    (в контексте `gsms` есть правило `_79XXXXXXXXX`), но полагаться на форму,
    которой никто не пользуется, незачем.
    """
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in "78":
        return "8" + digits[1:]
    if len(digits) == 10:
        return "8" + digits
    return digits


def place_call(employee, phone: str):
    """Звонок по клику: поднимаем трубку сотрудника, затем АТС набирает клиента.

    → (ok, сообщение). Исключения наружу не пускаем: кнопка в интерфейсе
    должна показать понятную причину, а не 500.
    """
    from django.conf import settings

    from .ami import AmiClient, AmiError

    if not employee or not employee.sip_extension:
        return False, "У вас не указан внутренний номер АТС — обратитесь к администратору"

    number = to_dial_format(phone)
    if len(number) < 6:
        return False, "Некорректный номер"

    client = AmiClient()
    if not client.is_configured():
        return False, "Телефония не настроена на этом сервере"

    caller = f'"{employee.user.get_full_name() or employee.sip_extension}" <{employee.sip_extension}>'
    try:
        with client as ami:
            resp = ami.originate(
                extension=employee.sip_extension,
                number=number,
                context=getattr(settings, "PBX_DIAL_CONTEXT", "lcsm"),
                caller_id=caller,
            )
    except AmiError as exc:
        logger.warning("звонок по клику не удался (%s → %s): %s",
                       employee.sip_extension, number, exc)
        return False, f"АТС недоступна: {exc}"
    except OSError as exc:
        logger.warning("нет связи с АТС: %s", exc)
        return False, "Нет связи с АТС"

    if (resp.get("Response") or "").lower() != "success":
        return False, resp.get("Message") or "АТС отклонила вызов"
    return True, "Поднимите трубку — АТС набирает номер"


# Насколько старым может быть звонок из CDR, чтобы завести по нему запись в
# реестре пропущенных. 🛑 Без порога первый же `--backfill` агента (13 тысяч
# исторических звонков) насыпал бы в реестр тысячи «новых» обращений
# двухлетней давности, и работать с ним стало бы невозможно.
MISSED_REGISTER_MAX_AGE_HOURS = 48


def _sync_callcenter_board(call) -> None:
    """Доска колл-центра: входящий с неизвестного номера → карточка.

    Это СТРАХОВОЧНЫЙ путь. Основной — слушатель AMI: он ставит карточку, пока
    телефон ещё звонит. Но до трубки доходит не всякий звонок (оборвался в
    голосовом меню — ``DialBegin`` не было вовсе), а CDR приходит на любой,
    поэтому дублируем здесь. Обе точки идемпотентны: карточка у клиента одна.

    🛑 Ошибки глотаем — доска не должна ронять приём звонков с АТС.
    """
    try:
        if call.direction != Call.DIRECTION_IN or call.client_id or not call.counterparty_phone:
            return
        from apps.callcenter.intake import handle_unknown_incoming_call

        # Сырой CallerID («"Иван" <89001234567>») — разбирает его intake:
        # здешний parse_clid понимает только внутренние трёхзначные номера.
        client = handle_unknown_incoming_call(
            call.counterparty_phone, clid_name=call.clid, started_at=call.started_at,
        )
        if client is not None:
            # Раз клиент теперь есть — привязываем к нему сам звонок, иначе
            # в журнале и в истории клиента разговор остался бы «ничьим».
            Call.objects.filter(pk=call.pk, client__isnull=True).update(client=client)
            call.client = client
    except Exception:  # noqa: BLE001
        logger.exception("колл-центр: не удалось обработать звонок %s",
                         getattr(call, "uniqueid", "?"))


def _sync_callcenter_result(call) -> None:
    """Спросить у оператора результат звонка (страховка к слушателю AMI).

    Слушатель ловит не всё: контейнер могли перезапустить, связь с АТС
    могла оборваться. CDR приходит на каждый звонок, поэтому дублируем.
    Идемпотентно по ключу «uniqueid + внутренний номер».

    🛑 Ошибки глотаем — приём звонков с АТС важнее модалки.
    """
    try:
        from apps.callcenter.calls import link_call

        link_call(call)
    except Exception:  # noqa: BLE001
        logger.exception("колл-центр: результат звонка %s не запрошен",
                         getattr(call, "uniqueid", "?"))


def _sync_missed_register(call) -> None:
    """CDR-страховка реестра пропущенных.

    Разговор состоялся → закрываем открытые обращения по этому номеру
    (перезвонили — долг снят). Не состоялся → заводим запись, если её ещё
    не создал диалплан.

    🛑 Реестр не должен ронять приём звонков с АТС: любая ошибка глотается.
    """
    from django.utils import timezone as _tz

    try:
        if (_tz.now() - call.started_at).total_seconds() > MISSED_REGISTER_MAX_AGE_HOURS * 3600:
            return
        from . import missed as missed_mod

        if call.outcome == Call.OUTCOME_ANSWERED:
            if call.counterparty_phone:
                missed_mod.close_open_for_phone(call.counterparty_phone, by_call=call)
            return
        if call.direction == Call.DIRECTION_IN:
            missed_mod.ensure_from_call(call)
    except Exception:  # noqa: BLE001
        logger.exception("реестр пропущенных: не удалось обработать звонок %s",
                         getattr(call, "uniqueid", "?"))
