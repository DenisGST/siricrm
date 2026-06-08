# -*- coding: utf-8 -*-
"""Сборка контекста заявления о банкротстве из Client + Region + LegalEntity + анкеты.

build_isk_context(service, overrides, sro) -> (ctx, flags, creditors, warnings)
  ctx        — плоский dict плейсхолдеров {key: str} для движка секций.
  flags      — dict булевых флагов для include_condition разделов.
  creditors  — список резолвленных кредиторов (для экрана дозаполнения).
  warnings   — список текстовых предупреждений (нет реквизитов кредитора и т.п.).

overrides (с экрана дозаполнения) переопределяют/дополняют авто-значения:
  debtor_full_prev, accounts_block, property_text, deals_text, income_text,
  family_text, employer, former_name, и т.д.
"""
import datetime
import re
from decimal import Decimal, InvalidOperation

from apps.crm.models import LegalEntity


# ── форматирование ──────────────────────────────────────────────────────────
def parse_money(raw):
    if raw is None:
        return None
    s = str(raw).strip().replace("\xa0", " ")
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    s = s.replace(" ", "")
    # десятичный разделитель — последняя запятая/точка с 1-2 цифрами после
    m = re.search(r"[.,](\d{1,2})$", s)
    if m:
        s = s[: m.start()].replace(",", "").replace(".", "") + "." + m.group(1)
    else:
        s = s.replace(",", "").replace(".", "")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def fmt_money(value):
    """Decimal/str → «26 300 руб.» или «6 532 руб. 28 коп.»"""
    d = value if isinstance(value, Decimal) else parse_money(value)
    if d is None:
        return "0 руб."
    rub = int(d)
    kop = int((d - rub) * 100)
    out = f"{rub:,}".replace(",", " ") + " руб."
    if kop:
        out += f" {kop:02d} коп."
    return out


def fmt_date(s):
    if not s:
        return ""
    if isinstance(s, (datetime.date, datetime.datetime)):
        return s.strftime("%d.%m.%Y")
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    return s


# ── анкета ──────────────────────────────────────────────────────────────────
def latest_response(service):
    return (service.questionnaire_responses
            .order_by("-is_complete", "-updated_at").first())


def answers_by_type(response):
    """{question_type: value_dict} — берём первый ответ каждого типа."""
    out = {}
    if response is None:
        return out
    for a in response.answers.select_related("question"):
        qt = a.question.question_type
        if qt not in out and isinstance(a.value, dict):
            out[qt] = a.value
    return out


def _resolve_le(le_id, name):
    le = None
    if le_id:
        le = LegalEntity.objects.filter(pk=le_id).first()
    if le is None and name:
        le = LegalEntity.objects.filter(name__iexact=name).first() \
            or LegalEntity.objects.filter(short_name__iexact=name).first() \
            or LegalEntity.objects.filter(name__icontains=name[:40]).first()
    return le


def _creditor_from_entry(name_id_field, name_field, entry):
    le = _resolve_le(entry.get(name_id_field), entry.get(name_field))
    if le:
        disp = le.name or le.short_name
        return {
            "name": disp, "ogrn": le.ogrn, "inn": le.inn,
            "address": (le.legal_address or "").strip(),
            "has_requisites": bool(le.legal_address and (le.ogrn or le.inn)),
            "le_id": str(le.id),
        }
    return {
        "name": entry.get(name_field) or "—", "ogrn": "", "inn": "",
        "address": "", "has_requisites": False, "le_id": "",
    }


def resolve_creditors(at):
    """Список кредиторов с реквизитами (банки+МФО+маркетплейсы+коммуналка+суд+штрафы+прочее).

    at — результат answers_by_type(response).
    Каждый элемент: name, ogrn, inn, address, has_requisites, amount(Decimal),
    basis (текст основания), date (str), kind.
    """
    out = []

    for e in (at.get("bank_debts", {}) or {}).get("entries", []):
        c = _creditor_from_entry("bank_id", "bank_name", e)
        amt = parse_money(e.get("balance") or e.get("loan_amount"))
        basis = "кредитного договора"
        if (e.get("enforcement") == "yes") or (e.get("court_decision") == "yes"):
            basis = "судебного акта"
        c.update(amount=amt, basis=basis, date=fmt_date(e.get("date_taken")),
                 overdue=(e.get("overdue") == "yes"), kind="bank")
        out.append(c)

    mfo = at.get("mfo_debts", {}) or {}
    if mfo.get("mode") != "unknown":
        for e in mfo.get("entries", []):
            c = _creditor_from_entry("mfo_id", "mfo_name", e)
            amt = parse_money(e.get("balance") or e.get("loan_amount"))
            c.update(amount=amt, basis="договора займа",
                     date=fmt_date(e.get("date_taken")),
                     overdue=(e.get("overdue") == "yes"), kind="mfo")
            out.append(c)

    for e in (at.get("marketplace_debts", {}) or {}).get("entries", []):
        item = (e.get("item") or "").strip()
        basis = "договора купли-продажи товара в рассрочку"
        if item:
            basis += f" ({item})"
        out.append({"name": e.get("marketplace") or "—", "ogrn": "", "inn": "",
                    "address": "", "has_requisites": False, "le_id": "",
                    "amount": parse_money(e.get("amount")),
                    "basis": basis, "date": "",
                    "overdue": True, "kind": "marketplace"})

    for e in (at.get("utility_debts", {}) or {}).get("entries", []):
        out.append({"name": e.get("org_name") or "—", "ogrn": "", "inn": "",
                    "address": "", "has_requisites": False, "le_id": "",
                    "amount": parse_money(e.get("amount")),
                    "basis": "за коммунальные услуги", "date": "",
                    "overdue": True, "kind": "utility"})

    for e in (at.get("court_debts", {}) or {}).get("entries", []):
        out.append({"name": e.get("court_name") or "по решению суда", "ogrn": "",
                    "inn": "", "address": "", "has_requisites": False, "le_id": "",
                    "amount": parse_money(e.get("amount")),
                    "basis": "решения суда", "date": "",
                    "overdue": True, "kind": "court"})

    for e in (at.get("fine_debts", {}) or {}).get("entries", []):
        out.append({"name": e.get("agency") or "—", "ogrn": "", "inn": "",
                    "address": "", "has_requisites": False, "le_id": "",
                    "amount": parse_money(e.get("amount")),
                    "basis": "штрафа", "date": "", "overdue": True, "kind": "fine"})

    for e in (at.get("other_debts", {}) or {}).get("entries", []):
        out.append({"name": (e.get("essence") or "—")[:120], "ogrn": "", "inn": "",
                    "address": "", "has_requisites": False, "le_id": "",
                    "amount": parse_money(e.get("amount")),
                    "basis": "иного обязательства", "date": "",
                    "overdue": True, "kind": "other"})

    return out


# ── narrative-тексты ───────────────────────────────────────────────────────
def _family_text(at, overrides):
    if overrides.get("family_text"):
        return overrides["family_text"]
    ms = at.get("marital_status", {}) or {}
    married = ms.get("marital") == "married"
    base = ("Должник состоит в браке." if married
            else "Должник в браке не состоит.")
    ch = at.get("children_list", {}) or {}
    if ch.get("has_children") == "yes" and ch.get("entries"):
        base += " На иждивении находятся несовершеннолетние дети."
    else:
        base += " На иждивении несовершеннолетних детей не имеет."
    return base


def _property_text(at, overrides):
    if overrides.get("property_text"):
        return overrides["property_text"]
    pa = at.get("property_assets", {}) or {}
    if pa.get("has_assets") == "yes" and pa.get("entries"):
        # краткое перечисление — детали уточняются на экране дозаполнения / в Описи
        items = []
        for e in pa["entries"]:
            nm = (e.get("name") or "").strip()
            if nm:
                items.append(nm)
        if items:
            return ("На дату подачи заявления у должника на праве собственности "
                    "имеется следующее имущество: " + "; ".join(items) + ".")
    return ("На дату подачи заявления в суд какого-либо недвижимого или движимого "
            "имущества, принадлежащего заявителю на праве индивидуальной, долевой "
            "и совместной собственности, должник не имеет, что подтверждается "
            "ответом на запрос Федеральной службы государственной регистрации, "
            "кадастра и картографии.")


def _deals_text(at, overrides):
    if overrides.get("deals_text"):
        return overrides["deals_text"]
    sa = at.get("sold_assets", {}) or {}
    if sa.get("has_sold") == "yes" and sa.get("entries"):
        items = [(e.get("name") or "").strip() for e in sa["entries"] if e.get("name")]
        if items:
            return ("В течение трех лет до даты обращения с настоящим заявлением "
                    "должником совершены сделки с имуществом: " + "; ".join(items) + ".")
    return ("Какие-либо сделки с недвижимым и движимым имуществом, ценными бумагами, "
            "долями в уставном капитале, а также сделки на сумму свыше трехсот тысяч "
            "рублей в течение трех лет до даты обращения с настоящим заявлением в суд "
            "должником не совершались.")


def _income_text(overrides):
    if overrides.get("income_text"):
        return overrides["income_text"]
    employer = (overrides.get("employer") or "").strip()
    if employer:
        return (f"В настоящее время должник трудоустроен в {employer}. "
                "Единственным доходом у должника является заработная плата.")
    return ("В настоящее время должник не трудоустроен. "
            "Ежемесячный доход у должника отсутствует.")


def _ufns_for_region(region):
    if region is None:
        return None
    qs = LegalEntity.objects.filter(kind__short_name__icontains="ФНС")
    le = qs.filter(region=region).first()
    if le is None and region.name:
        le = qs.filter(name__icontains=region.name.split()[0]).first()
    return le


def _debtor_names(client, overrides):
    full = " ".join(p for p in [client.last_name, client.first_name,
                                client.patronymic] if p).strip()
    short = client.last_name or ""
    initials = "".join(f"{p[0]}." for p in [client.first_name, client.patronymic] if p)
    short = (short + " " + initials).strip()
    # прежняя фамилия: override → name_history → нет
    prev = (overrides.get("former_name") or "").strip()
    if not prev:
        nh = client.name_history.first() if hasattr(client, "name_history") else None
        if nh and nh.last_name and nh.last_name != client.last_name:
            prev = nh.last_name
    if prev:
        full_prev = " ".join(p for p in [
            f"{client.last_name} ({prev})", client.first_name, client.patronymic
        ] if p)
    else:
        full_prev = full
    return full, full_prev, short


# ── главный сборщик ─────────────────────────────────────────────────────────
def build_isk_context(service, overrides=None, sro=None, response=None):
    overrides = overrides or {}
    client = service.client
    region = service.region
    if response is None:
        response = latest_response(service)
    at = answers_by_type(response)

    creditors = resolve_creditors(at)
    warnings = [c["name"] for c in creditors
                if c["kind"] in ("bank", "mfo") and not c["has_requisites"]]

    # блок КРЕДИТОРЫ (реквизиты) — только те, у кого есть LegalEntity
    cred_lines = []
    seen = set()
    for c in creditors:
        key = (c["name"], c["inn"])
        if key in seen or not c["name"] or c["name"] == "—":
            continue
        seen.add(key)
        parts = [c["name"]]
        if c["ogrn"]:
            parts.append(f"ОГРН {c['ogrn']}")
        if c["inn"]:
            parts.append(f"ИНН {c['inn']}")
        line = " ".join(parts)
        if c["address"]:
            line += f"\n{c['address']}"
        cred_lines.append(line)
    creditors_block = "\n".join(cred_lines) if cred_lines else "—"

    # перечень обязательств
    debt_lines = []
    total = Decimal(0)
    overdue_total = Decimal(0)
    for i, c in enumerate(creditors, 1):
        amt = c.get("amount") or Decimal(0)
        total += amt
        if c.get("overdue"):
            overdue_total += amt
        basis = c["basis"]
        date = f" от {c['date']} г." if c.get("date") else ""
        line = (f"{i}) задолженность перед {c['name']} на основании {basis}{date} "
                f"в размере {fmt_money(amt)}")
        if c.get("overdue"):
            line += f", в том числе просроченная задолженность в размере {fmt_money(amt)}"
        debt_lines.append(line + ";")
    debts_list = "\n".join(debt_lines) if debt_lines else "—"

    pct = int(round(float(overdue_total / total * 100))) if total else 100

    # суд
    court_name = region.court_name if region and region.court_name else "Арбитражный суд"
    court_addr = ""
    if region and region.court_address_id:
        court_addr = (region.court_address.result or "").strip()
    ufns = _ufns_for_region(region)

    # СРО (просительная часть)
    if sro:
        sro_parts = [sro.name or sro.short_name]
        meta = []
        if sro.inn:
            meta.append(f"ИНН: {sro.inn}")
        if sro.ogrn:
            meta.append(f"ОГРН: {sro.ogrn}")
        if sro.legal_address:
            meta.append(sro.legal_address.strip())
        petition_sro = sro_parts[0] + (" (" + ", ".join(meta) + ")" if meta else "")
    else:
        petition_sro = overrides.get("petition_sro", "____________________")

    full, full_prev, short = _debtor_names(client, overrides)
    reg_addr = ""
    addr = client.addresses.filter(address_type="registration").first() \
        or client.addresses.filter(address_type="actual").first()
    if addr:
        reg_addr = (addr.result or "").strip()

    former = (overrides.get("former_name") or "").strip()
    if not former:
        nh = client.name_history.first() if hasattr(client, "name_history") else None
        if nh and nh.last_name and nh.last_name != client.last_name:
            former = nh.last_name

    ctx = {
        "court_name": court_name,
        "court_address": court_addr,
        "debtor_full": full,
        "debtor_full_prev": full_prev,
        "debtor_short": short,
        "debtor_last": client.last_name or "",
        "debtor_first": client.first_name or "",
        "debtor_patronymic": client.patronymic or "",
        "former_name": former,
        "inn": client.inn or "",
        "snils": client.snils or "",
        "birth_date": fmt_date(client.birth_date),
        "birth_place": client.birth_place or "",
        "reg_address": reg_addr,
        "passport_series": client.passport_series or "",
        "passport_number": client.passport_number or "",
        "passport_issued_by": client.passport_issued_by or "",
        "passport_issued_date": fmt_date(client.passport_issued_date),
        "passport_division_code": client.passport_division_code or "",
        "creditors_block": creditors_block,
        "ufns_name": (ufns.name if ufns else "УФНС России по субъекту РФ"),
        "ufns_address": (ufns.legal_address.strip() if ufns and ufns.legal_address else ""),
        "debts_list": debts_list,
        "sum_total": fmt_money(total).replace(" руб.", ""),
        "sum_overdue": fmt_money(overdue_total).replace(" руб.", ""),
        "overdue_pct": str(pct),
        "income_text": _income_text(overrides),
        "family_text": _family_text(at, overrides),
        "property_text": _property_text(at, overrides),
        "deals_text": _deals_text(at, overrides),
        "accounts_block": (overrides.get("accounts_block") or "").strip(),
        "petition_sro": petition_sro,
        "appendix_list": overrides.get("appendix_list", ""),
        "year": str(datetime.date.today().year),
    }

    flags = {
        "has_passport": bool(client.passport_series and client.passport_number),
        "has_accounts": bool(ctx["accounts_block"]),
        "has_income": bool((overrides.get("employer") or "").strip()),
        "is_married": (at.get("marital_status", {}) or {}).get("marital") == "married",
        "has_property": (at.get("property_assets", {}) or {}).get("has_assets") == "yes",
        "has_sold_assets": (at.get("sold_assets", {}) or {}).get("has_sold") == "yes",
        "has_children": (at.get("children_list", {}) or {}).get("has_children") == "yes",
    }
    return ctx, flags, creditors, warnings
