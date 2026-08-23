"""Разбор данных CDR: направление звонка, внутренний номер, привязка
к сотруднику и клиенту.

Правила направления намеренно строятся по НОМЕРАМ, а не по именам контекстов
диалплана: контексты на АТС правятся руками (inboundb/incallcc_b/osd_b/yuro_b…)
и могут добавиться новые, а «трёхзначный внутренний против длинного внешнего» —
свойство самой нумерации и не поедет.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.crm.phone_utils import find_client_by_phone, normalize_phone

from .models import Call

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


def upsert_call(row: dict) -> tuple:
    """Создать или обновить звонок по ``uniqueid``. → (Call, created)."""
    uniqueid = (row.get("uniqueid") or "").strip()[:32]
    if not uniqueid:
        raise ValueError("uniqueid обязателен")
    fields = build_call_fields(row)
    call, created = Call.objects.update_or_create(uniqueid=uniqueid, defaults=fields)
    return call, created
