"""Парсер пакета ответов ФНС по запросу АУ в деле о банкротстве.

Пакет — «портянка» из нескольких суб-документов, подписанных УКЭП:
  · СПРАВКА об исполнении обязанности по уплате налогов (КНД 1120101);
  · Сведения об участии субъекта запроса в юридических лицах;
  · Сведения о банковских счетах (вкладах, ЭСП)  ← ПОЛНЫЙ список (с закрытыми);
  · Форма 9ф — сведения об ОТКРЫТЫХ счетах       ← подмножество полного;
  · Сведения об административных правонарушениях;
  · Сведения об объектах налогообложения (недвижимость / земля / транспорт);
  · Справки о доходах 2-НДФЛ (КНД 1175018) — несколько, за разные годы/агентов.

🛑 Особенности реальных справок (проверено на 7 образцах разных УФНС):
  · блок банка рвётся границей страницы (шапка банка не повторяется) — парсим
    сквозным потоком с состоянием «текущий банк», не постранично;
  · субъект сведений может быть НЕ должником, а его СУПРУГОМ («Тип субъекта
    запроса: Супруг(супруга) должника ФЛ») — ФИО в справке тогда чужое, это норма;
  · полной секции счетов может не быть вообще — только Форма 9ф (тогда все
    найденные счета открыты);
  · секции бывают ПРОДУБЛИРОВАНЫ в одном файле — дедуп по номеру счёта;
  · номера ЭСП не 20-значные: «24A-0265256652», «203422738», «5377855936»,
    «00000000200004309644» (с ведущими нулями);
  · многострочные ячейки состояния рвутся на слоги: «прекращено право
    использовани / я» — склеиваем и сравниваем без пробелов;
  · у закрытого счёта «Вид счёта» бывает пустым.

Счета берём из ТЕКСТОВОГО слоя (он полный и надёжный), объекты имущества — из
табличного (в тексте они склеиваются в кашу и обрезаются по колонкам).
"""
from __future__ import annotations

import io
import re
from datetime import datetime

DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
# Номер счёта/ЭСП: начинается с цифры, дальше цифры/латиница/дефис. Не дата.
ACC_RE = re.compile(
    r"^([0-9][0-9A-ZА-Я\-]{5,24})\s+(\d{2}\.\d{2}\.\d{4})"
    r"(?:\s+(\d{2}\.\d{2}\.\d{4}))?\s*(.*)$"
)
REGNOM_RE = re.compile(r"^РегНом/НомФ:\s*(.*)$")
# Начало названия банка/НКО — им же ограничиваем сбор многострочных ячеек.
ORG_START_RE = re.compile(
    r"^(публичн|акционерн|общество|банк|коммерческ|небанковск|"
    r"ооо|оао|пао|ао\s|нко|государственн)", re.I
)
INNKPP_RE = re.compile(r"^ИНН/КПП:\s*(\d{10,12})\s*/\s*(\d{0,9})")
BIK_RE = re.compile(r"^БИК\s*(?:\(СВИФТ\))?\s*:\s*(\S+)")
ADDR_RE = re.compile(r"^Адрес:\s*(.*)$")

# Состояния счёта как в справке → код модели BankAccount.
STATES = [
    ("прекращено право использования", "revoked"),
    ("предоставлено право использования", "granted"),
    ("в ликвидированном банке", "liq_bank"),
    ("открыт", "open"),
    ("закрыт", "closed"),
]

# Мусор УКЭП-штампа и сноски — наслаиваются на текст каждой страницы.
SKIP_PREFIXES = (
    "Сертификат:", "Владелец:", "Действителен:", "ДОКУМЕНТ ПОДПИСАН",
    "УСИЛЕННОЙ КВАЛИФИЦИРОВАННОЙ", "ЭЛЕКТРОННОЙ ПОДПИСЬЮ",
    "НАЛОГОВОЙ СЛУЖБЫ ПО ЦЕНТРАЛИЗОВАННОЙ", "ДАННЫХ",
    "Сведения об открытии или о закрытии",
    "юридических и физических лиц поступают",
    "Федерации от банков",
    "являющихся индивидуальными предпринимателями",
    "информацией о счетах физических лиц",
    "Сведения о зарегистрированных правах",
    "налоговые органы в соответствии со ст. 85",
    "недвижимое имущество, регистрацию транспортных",
    "Налоговые органы не являются первоисточником",
    "(полное наименование налогового органа)",
)

# Заголовки суб-документов пакета.
SECTIONS = [
    ("accounts_full", "Сведения о банковских счетах"),
    ("accounts_9f", "Сведения об открытых банковских счетах"),
    ("income", "СПРАВКА О ДОХОДАХ И СУММАХ НАЛОГА"),
    ("objects", "Сведения о наличии объектов налогообложения"),
    ("legal_entities", "Сведения об участии субъекта запроса в юридических лицах"),
    ("admin", "Сведения об административных правонарушениях"),
    ("tax_debt", "об исполнении налогоплательщиком"),
    ("tax_debt", "Справка об исполнении обязанности по уплате"),
]

NO_DATA = "запрашиваемые сведения отсутствуют"


class FnsParseError(Exception):
    """Файл не похож на пакет ответов ФНС / не читается."""


# ── Утилиты ────────────────────────────────────────────────────────────────

def _d(value: str | None):
    """«07.07.2026» → «2026-07-07» (ISO, для JSON). Пусто → None."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _num(value: str | None):
    """«829 123,57» → «829123.57» (строка — Decimal собирём при сохранении)."""
    if not value:
        return None
    s = re.sub(r"[^\d,.\-]", "", value).replace(",", ".")
    if not s or s in (".", "-"):
        return None
    try:
        float(s)
    except ValueError:
        return None
    return s


def _flat(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _clean_lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(line.startswith(p) for p in SKIP_PREFIXES):
            continue
        out.append(line)
    return out


def _split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Режем пакет на суб-документы по заголовкам. Секции могут повторяться."""
    sections: list[tuple[str, list[str]]] = []
    cur_kind: str | None = None
    cur: list[str] = []
    for line in lines:
        kind = None
        for k, marker in SECTIONS:
            if marker in line:
                kind = k
                break
        if kind:
            if cur_kind:
                sections.append((cur_kind, cur))
            cur_kind, cur = kind, [line]
            continue
        if cur_kind:
            cur.append(line)
    if cur_kind:
        sections.append((cur_kind, cur))
    return sections


# ── Счета ──────────────────────────────────────────────────────────────────

def _state_and_kind(blob: str) -> tuple[str, str, str]:
    """«прекращено право использовани я ЭСП, не являющееся корпоративным»
    → («revoked», «прекращено право использования», «ЭСП, не являющееся…»).

    Ячейка состояния в PDF многострочная и рвётся посреди слова — поэтому
    сравниваем склейку слов БЕЗ пробелов, жадно съедая префикс.
    """
    words = _flat(blob).split()
    if not words:
        return "", "", ""
    for text, code in STATES:
        target = re.sub(r"\s+", "", text).lower()
        glued = ""
        for i, w in enumerate(words):
            glued += w.lower()
            if glued == target:
                return code, text, " ".join(words[i + 1:]).strip()
            if not target.startswith(glued):
                break
    return "", "", " ".join(words).strip()


def _bank_name(pending: list[str]) -> str:
    """Имя банка — 1–2 строки прямо перед «РегНом/НомФ:».

    Выше в буфере лежит мусор шапки таблицы («Номер счета/номер ЭСП», «Дата»,
    «открытия/да», …), поэтому идём с конца до строки, похожей на начало
    названия организации («Публичное акционерное общество…», «Банк ВТБ…»).
    """
    buf: list[str] = []
    for line in reversed(pending[-4:]):
        buf.insert(0, line)
        if ORG_START_RE.match(line):
            return _flat(" ".join(buf))
    return _flat(pending[-1]) if pending else ""


def _parse_accounts(lines: list[str], *, default_state: str = "") -> list[dict]:
    """Секция счетов: блоки банков + строки счетов. Сквозной проход."""
    accounts: list[dict] = []
    bank: dict | None = None
    pending_name: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Сведения сформированы"):
            break

        m = REGNOM_RE.match(line)
        if m:  # начало блока банка — имя копилось выше
            bank = {
                "name": _bank_name(pending_name),
                "regnum": m.group(1).strip(),
                "inn": "", "kpp": "", "bik": "", "address": "",
            }
            pending_name = []
            i += 1
            addr: list[str] = []
            while i < len(lines):  # реквизиты банка — до первой строки счёта
                x = lines[i]
                if ACC_RE.match(x) or REGNOM_RE.match(x):
                    break
                if (mm := INNKPP_RE.match(x)):
                    bank["inn"], bank["kpp"] = mm.group(1), mm.group(2)
                elif (mm := BIK_RE.match(x)):
                    bank["bik"] = mm.group(1)
                elif (mm := ADDR_RE.match(x)):
                    addr = [mm.group(1)]
                elif addr:
                    addr.append(x)
                i += 1
            bank["address"] = _flat(" ".join(addr))
            continue

        m = ACC_RE.match(line)
        if m and bank is not None:
            number, opened, closed, tail = m.groups()
            # Ячейки «Состояние» и «Вид счёта» многострочные — хвост уезжает на
            # следующие строки. Собираем, пока не упрёмся в новый счёт, новый
            # банк или дату (границы ячейки в тексте не видно).
            j, buf = i + 1, [tail or ""]
            while j < len(lines) and len(buf) < 8:
                x = lines[j]
                if (ACC_RE.match(x) or REGNOM_RE.match(x) or ORG_START_RE.match(x)
                        or DATE_RE.search(x) or len(x) > 80
                        or x.startswith("Сведения сформированы")):
                    break
                buf.append(x)
                j += 1
            state, state_text, kind = _state_and_kind(" ".join(buf))
            if not state:
                state, state_text = default_state, ""
                kind = _flat(" ".join(buf))
            accounts.append({
                "number": number,
                "opened_date": _d(opened),
                "closed_date": _d(closed),
                "state": state,
                "state_text": state_text,
                "account_kind": kind,
                "bank_name": bank["name"],
                "bank_regnum": bank["regnum"],
                "bank_inn": bank["inn"],
                "bank_kpp": bank["kpp"],
                "bank_bik": bank["bik"],
                "bank_address": bank["address"],
            })
            i = j
            pending_name = []
            continue

        pending_name.append(line)  # мусор шапки отсеет _bank_name
        i += 1
    return accounts


ACC_NUM_RE = re.compile(r"^[0-9][0-9A-ZА-Я\-]{5,24}$")


def _bank_from_cell(cell: str, pending_name: str = "") -> dict:
    """Шапка банка приходит ОДНОЙ ячейкой таблицы (имя + РегНом + ИНН/КПП + БИК + адрес)."""
    bank = {"name": "", "regnum": "", "inn": "", "kpp": "", "bik": "", "address": ""}
    name: list[str] = []
    addr: list[str] = []
    started = False
    for line in [l.strip() for l in cell.splitlines() if l.strip()]:
        if (m := REGNOM_RE.match(line)):
            bank["regnum"] = m.group(1).strip()
            started = True
        elif (m := INNKPP_RE.match(line)):
            bank["inn"], bank["kpp"] = m.group(1), m.group(2)
        elif (m := BIK_RE.match(line)):
            bank["bik"] = m.group(1)
        elif (m := ADDR_RE.match(line)):
            addr = [m.group(1)]
        elif addr:
            addr.append(line)
        elif not started:
            name.append(line)
    bank["name"] = _flat(" ".join(name)) or _flat(pending_name)
    bank["address"] = _flat(" ".join(addr))
    return bank


def _parse_account_tables(pdf) -> dict[str, list[dict]]:
    """Счета из ТАБЛИЧНОГО слоя — основной путь.

    🛑 В тексте многострочные ячейки («прекращено / право / использовани / я» и
    «ЭСП, не являющееся / корпоративным») перемешиваются между собой и со строкой
    счёта — собрать их обратно из текста нельзя. Таблица отдаёт ячейку целиком.
    Полная секция — 5 колонок, Форма 9ф — 3 (номер, дата открытия, вид счёта).
    """
    out: dict[str, list[dict]] = {"full": [], "9f": []}
    bank: dict | None = None
    pending_name = ""
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            if not table:
                continue
            ncols = max(len(r) for r in table)
            if ncols not in (3, 5):
                continue
            # Таблица счетов, а не «Сведения о документах» из адм. правонарушений.
            if not any(("РегНом/НомФ" in (r[0] or "")) or ACC_NUM_RE.match(_flat(r[0]))
                       for r in table):
                continue
            mode = "full" if ncols == 5 else "9f"
            for row in table:
                cell0 = (row[0] or "").strip()
                if not cell0 or cell0.startswith("Номер счета"):
                    continue
                if "РегНом/НомФ" in cell0:
                    bank = _bank_from_cell(cell0, pending_name)
                    pending_name = ""
                    continue
                if not ACC_NUM_RE.match(_flat(cell0)):
                    # Имя банка уехало на предыдущую страницу — реквизиты придут ниже.
                    pending_name = cell0
                    continue
                if bank is None:
                    continue
                if mode == "full":
                    opened, closed = _flat(row[1]), _flat(row[2])
                    state_cell, kind_cell = row[3], row[4]
                else:
                    opened, closed = _flat(row[1]), ""
                    state_cell, kind_cell = "открыт", row[2]
                state, state_text, _ = _state_and_kind(state_cell or "")
                out[mode].append({
                    "number": _flat(cell0),
                    "opened_date": _d(opened),
                    "closed_date": _d(closed),
                    "state": state,
                    "state_text": state_text if mode == "full" else "",
                    "account_kind": _flat(kind_cell),
                    "bank_name": bank["name"],
                    "bank_regnum": bank["regnum"],
                    "bank_inn": bank["inn"],
                    "bank_kpp": bank["kpp"],
                    "bank_bik": bank["bik"],
                    "bank_address": bank["address"],
                })
    return out


def _merge_accounts(full: list[dict], short: list[dict]) -> list[dict]:
    """Полная секция — надмножество Формы 9ф. Дедуп по номеру счёта."""
    merged: dict[str, dict] = {}
    for acc in full + short:
        key = acc["number"]
        if key in merged:
            # Полная запись (с состоянием/датой закрытия) приоритетнее 9ф.
            if not merged[key].get("state_text") and acc.get("state_text"):
                merged[key] = acc
            continue
        merged[key] = acc
    return list(merged.values())


# ── 2-НДФЛ ─────────────────────────────────────────────────────────────────

def _parse_income(lines: list[str]) -> dict | None:
    text = "\n".join(lines)
    m = re.search(r"за\s+(\d{4})\s+год\s+от\s+(\d{2}\.\d{2}\.\d{4})", text)
    if not m:
        return None
    cert = {"year": int(m.group(1)), "cert_date": _d(m.group(2))}

    m = re.search(r"Код по ОКТМО\s+(\d+)", text)
    cert["oktmo"] = m.group(1) if m else ""
    m = re.search(r"ИНН\s+(\d{10,12})\s+КПП\s*(\d{9})?", text)
    cert["agent_inn"] = m.group(1) if m else ""
    cert["agent_kpp"] = (m.group(2) or "") if m else ""

    # 🛑 Наименование агента может стоять и ПЕРЕД подписью «Налоговый агент», и
    # после неё (подпись — левая колонка формы, длинное имя обтекает её сверху и
    # снизу), и на одной строке с ней. Собираем все три куска.
    name: list[str] = []
    for i, line in enumerate(lines):
        if not line.startswith("Налоговый агент"):
            continue
        prev = lines[i - 1] if i else ""
        if prev and not prev.startswith(("Код по ОКТМО", "1. Сведения", "Форма по КНД",
                                         "СПРАВКА", "за ", "ИНН/КПП")):
            name.append(prev)
        inline = line.split("Налоговый агент", 1)[1].strip()
        if inline:
            name.append(inline)
        for nxt in lines[i + 1:]:
            if nxt.startswith(("Форма реорганизации", "ИНН/КПП реорганизованной", "2.")):
                break
            name.append(nxt)
        break
    cert["agent_name"] = _flat(" ".join(name))

    for key, pattern in (
        ("total_income", r"Общая сумма дохода\s+([\d\s.,]+?)\s+Налоговая база\s+([\d\s.,]+)"),
        ("tax_calculated", r"Сумма налога исчисленная\s+([\d\s.,]+)"),
        ("tax_withheld", r"Сумма налога удержанная\s+([\d\s.,]+)"),
    ):
        m = re.search(pattern, text)
        if not m:
            cert[key] = None
            continue
        cert[key] = _num(m.group(1))
        if key == "total_income":
            cert["tax_base"] = _num(m.group(2))
    cert.setdefault("tax_base", None)

    # Строки доходов: «месяц код сумма [код_вычета сумма_вычета]».
    rows = []
    inside = False
    for line in lines:
        if line.startswith("3. Доходы"):
            inside = True
            continue
        if inside and line.startswith(("4. Стандартные", "5. Общая")):
            break
        if not inside or line.startswith("Месяц"):
            continue
        m = re.match(r"^(\d{1,2})\s+(\d{4})\s+([\d\s.,]+?)(?:\s+(\d{3})\s+([\d\s.,]+))?$", line)
        if m:
            rows.append({
                "month": int(m.group(1)), "code": m.group(2),
                "amount": _num(m.group(3)),
                "deduction_code": m.group(4) or "",
                "deduction": _num(m.group(5)),
            })
    cert["rows"] = rows
    return cert


# ── Объекты налогообложения (таблицы) ──────────────────────────────────────

def _norm_header(cell) -> str:
    return _flat(cell or "").lower()


def _table_kind(header: list[str]) -> str | None:
    joined = " | ".join(header)
    if "вид объекта" in joined and "кадастровый" in joined:
        return "realty"
    if "категория" in joined and "земли" in joined:
        return "land"
    if "марка" in joined and ("vin" in joined or "птс" in joined):
        return "vehicles"
    return None


def _col(header: list[str], *needles: str) -> int | None:
    for idx, cell in enumerate(header):
        if all(n in cell for n in needles):
            return idx
    return None


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return _flat(row[idx])


def _dedupe(items: list[dict], *keys: str) -> list[dict]:
    """Секции пакета бывают продублированы в одном файле — схлопываем по ключу."""
    seen, out = set(), []
    for item in items:
        key = tuple(item.get(k) or "" for k in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _parse_object_tables(pdf) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"realty": [], "land": [], "vehicles": []}
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            if not table or len(table) < 2:
                continue
            header = [_norm_header(c) for c in table[0]]
            kind = _table_kind(header)
            if not kind:
                continue
            for row in table[1:]:
                if not row or not _flat(row[0]) or not _flat(row[0]).rstrip(".").isdigit():
                    continue  # не строка данных (номер п/п в первой колонке)
                if kind == "realty":
                    out["realty"].append({
                        "object_type": _cell(row, _col(header, "вид объекта")),
                        "address": _cell(row, _col(header, "адрес")),
                        "area": _cell(row, _col(header, "площадь")),
                        "share": _cell(row, _col(header, "доли")),
                        "cadastral_number": _cell(row, _col(header, "кадастровый номер")),
                        "cadastral_value": _num(_cell(row, _col(header, "кадастровая стоимость"))),
                        "commissioned_date": _d(_cell(row, _col(header, "ввода в эксплуатацию"))),
                        "reg_date": _d(_cell(row, _col(header, "регистрации владения"))),
                        "dereg_date": _d(_cell(row, _col(header, "прекращения владения"))),
                    })
                elif kind == "land":
                    out["land"].append({
                        "category": _cell(row, _col(header, "категория")),
                        "address": _cell(row, _col(header, "адрес")),
                        "area": _cell(row, _col(header, "площадь")),
                        "share": _cell(row, _col(header, "доли")),
                        "cadastral_number": _cell(row, _col(header, "кадастровый номер")),
                        "cadastral_value": _num(_cell(row, _col(header, "кадастровая стоимость"))),
                        "reg_date": _d(_cell(row, _col(header, "регистрации владения"))),
                        "dereg_date": _d(_cell(row, _col(header, "прекращения владения"))),
                    })
                else:
                    out["vehicles"].append({
                        "ownership_kind": _cell(row, _col(header, "вид", "собственности")),
                        "year": _cell(row, _col(header, "год")),
                        "model": _cell(row, _col(header, "марка")),
                        "power": _cell(row, _col(header, "мощность")),
                        "reg_authority": _cell(row, _col(header, "регистрирую")),
                        "plate": _cell(row, _col(header, "знак")),
                        "vin": _cell(row, _col(header, "vin")),
                        "pts": _cell(row, _col(header, "птс")),
                        "reg_date": _d(_cell(row, _col(header, "регистрации владения"))),
                        "dereg_date": _d(_cell(row, _col(header, "прекращения владения"))),
                    })
    return {
        "realty": _dedupe(out["realty"], "cadastral_number", "address"),
        "land": _dedupe(out["land"], "cadastral_number", "address"),
        "vehicles": _dedupe(out["vehicles"], "vin", "plate"),
    }


# ── Шапка: субъект / должник / дело ────────────────────────────────────────

def _after(lines: list[str], prefix: str) -> str:
    for line in lines:
        if line.startswith(prefix):
            return line.split(prefix, 1)[1].strip(": ").strip()
    return ""


def _parse_header(lines: list[str]) -> dict:
    subject_type_raw = _after(lines, "Тип субъекта запроса")
    subject = "spouse" if "упруг" in subject_type_raw else "debtor"

    head = {
        "subject": subject,
        "subject_type_raw": subject_type_raw,
        "subject_fio": _after(lines, "ФИО субъекта запроса"),
        "subject_inn": _after(lines, "ИНН субъекта запроса"),
        "subject_birth_date": None,
        "debtor_fio": _after(lines, "ФИО должника"),
        "debtor_inn": _after(lines, "ИНН должника"),
        "court_name": _after(lines, "Наименование суда"),
        "case_number": _after(lines, "Номер дела о банкротстве"),
        "formed_at": None,
        "tax_authority": "",
    }

    # Шапка секции счетов: ФИО КАПСОМ, «ИНН: X», «Дата рождения: dd.mm.yyyy, …».
    for i, line in enumerate(lines):
        if line.startswith("ИНН:") and i:
            inn = line.split(":", 1)[1].strip()
            if not head["subject_inn"]:
                head["subject_inn"] = inn
            if not head["subject_fio"]:
                head["subject_fio"] = lines[i - 1].strip()
        if line.startswith("Дата рождения:") and not head["subject_birth_date"]:
            m = DATE_RE.search(line)
            if m:
                head["subject_birth_date"] = _d(m.group(0))

    for line in lines:
        if line.startswith("Дата формирования"):
            m = DATE_RE.search(line)
            if m:
                head["formed_at"] = _d(m.group(0))
                break

    # 🛑 «Сведения сформированы» — подпись левой колонки: название органа обтекает
    # её и сверху, и снизу (как «Налоговый агент» в 2-НДФЛ).
    for i, line in enumerate(lines):
        if not line.startswith("Сведения сформированы"):
            continue
        parts = []
        prev = lines[i - 1] if i else ""
        if re.match(r"^(Управление|Межрайонная|Межрегиональная|Инспекция) ", prev):
            parts.append(prev)
        inline = line.split("Сведения сформированы", 1)[1].strip()
        if inline:
            parts.append(inline)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        is_next_section = (nxt.startswith(("Сведения", "(полное", "Форма", "СПРАВКА", "Код по КНД"))
                           or any(marker in nxt for _, marker in SECTIONS))
        if nxt and not is_next_section:
            parts.append(nxt)
        head["tax_authority"] = _flat(" ".join(parts))
        break
    if not head["tax_authority"]:
        for line in lines:
            if re.match(r"^(Управление|Межрайонная инспекция) Федеральной налоговой службы", line):
                head["tax_authority"] = _flat(line)
                break
    return head


def _parse_admin(lines: list[str]) -> list[dict]:
    """Административные правонарушения — нумерованные пункты с деталями."""
    if any(NO_DATA in line for line in lines):
        return []
    items: list[dict] = []
    cur: dict | None = None
    for line in lines:
        m = re.match(r"^(\d+)\.\s+(.+)$", line)
        if m and not line.startswith(("1. Сведения", "2. Сведения", "3. Сведения")):
            if cur:
                items.append(cur)
            cur = {"title": m.group(2).strip(), "details": []}
            continue
        if cur is not None:
            cur["details"].append(line)
    if cur:
        items.append(cur)
    out = []
    for it in items:
        details = [d for d in it["details"] if not d.startswith(("Сведения о документах", "Вид документа"))]
        date = None
        for d in details:
            if d.startswith("Дата вступления в силу"):
                m = DATE_RE.search(d)
                date = _d(m.group(0)) if m else None
        amount = None
        for d in details:
            if d.startswith("Сумма назначенного штрафа"):
                amount = _num(d)
        out.append({
            "title": _flat(it["title"]),
            "details": "\n".join(details),
            "date": date,
            "amount": amount,
        })
    return out


# ── Точка входа ────────────────────────────────────────────────────────────

def analyze_stream(text: str, objects_getter=None, accounts_getter=None):
    """Разбор пакета. Генератор шагов: {"log": …} …, в конце {"result": …}.

    `accounts_getter` / `objects_getter` — callable'ы, достающие счета и имущество
    из ТАБЛИЧНОГО слоя PDF (см. `_parse_account_tables` / `_parse_object_tables`).
    Табличный слой — основной: в тексте многострочные ячейки перемешиваются.
    Без getter'ов счета разбираются из текста (фолбэк + тесты на фикстурах),
    а имущество остаётся пустым.
    """
    if not text.strip():
        raise FnsParseError(
            "В PDF нет текстового слоя (похоже на скан). "
            "Загрузите оригинал справки из личного кабинета ФНС, а не скан."
        )

    lines = _clean_lines(text)
    if not any("субъекта запроса" in line or "Сведения о банковских счетах" in line
               or "Форма 9ф" in line for line in lines):
        raise FnsParseError("Это не похоже на пакет сведений ФНС по делу о банкротстве.")
    yield {"log": "Документ распознан: пакет сведений ФНС (АИС «Налог»)", "ok": True}

    head = _parse_header(lines)
    who = "должник" if head["subject"] == "debtor" else "СУПРУГ(А) должника"
    yield {"log": f"Субъект сведений: {head['subject_fio']} · ИНН {head['subject_inn']} — {who}",
           "ok": True}
    if head["debtor_fio"]:
        yield {"log": f"Должник по справке: {head['debtor_fio']} · ИНН {head['debtor_inn']}"}
    if head["case_number"]:
        yield {"log": f"Дело о банкротстве: {head['case_number']} · {head['court_name']}"}
    if head["formed_at"]:
        yield {"log": f"Сведения сформированы: {head['formed_at']} · {head['tax_authority']}"}

    sections = _split_sections(lines)

    # ── Счета: сперва таблицы (там ячейки целые), текст — фолбэк ──
    full_acc: list[dict] = []
    short_acc: list[dict] = []
    if accounts_getter:
        tabled = accounts_getter()
        full_acc = _dedupe(tabled["full"], "number")
        short_acc = _dedupe(tabled["9f"], "number")
        if full_acc:
            yield {"log": f"Секция «Сведения о банковских счетах» — найдено счетов: {len(full_acc)}"}
        if short_acc:
            yield {"log": f"Форма 9ф (только открытые) — найдено: {len(short_acc)}"}
    if not full_acc and not short_acc:
        if accounts_getter:
            yield {"log": "Таблицы счетов не читаются — разбираю текстом", "warn": True}
        for kind, sec in sections:
            if kind == "accounts_full":
                got = _parse_accounts(sec)
                full_acc += got
                yield {"log": f"Секция «Сведения о банковских счетах» — найдено счетов: {len(got)}"}
            elif kind == "accounts_9f":
                got = _parse_accounts(sec, default_state="open")
                short_acc += got
                yield {"log": f"Форма 9ф (только открытые) — найдено: {len(got)}"}
    accounts = _merge_accounts(full_acc, short_acc)
    banks = {a["bank_inn"] or a["bank_name"] for a in accounts}
    opened = sum(1 for a in accounts if a["state"] in ("open", "granted"))
    if accounts:
        yield {"log": f"Итого счетов: {len(accounts)} (действующих {opened}) "
                      f"в {len(banks)} банках/НКО", "ok": True}
    else:
        yield {"log": "Счета в справке не найдены", "warn": True}

    # ── Объекты налогообложения (из таблиц) ──
    objects = objects_getter() if objects_getter else {"realty": [], "land": [], "vehicles": []}
    realty, land, vehicles = objects["realty"], objects["land"], objects["vehicles"]
    yield {"log": f"Недвижимость: {len(realty)} · Земельные участки: {len(land)} · "
                  f"Транспорт: {len(vehicles)}", "ok": bool(realty or land or vehicles)}

    # ── 2-НДФЛ ──
    incomes: list[dict] = []
    seen: set = set()
    for kind, sec in sections:
        if kind != "income":
            continue
        cert = _parse_income(sec)
        if not cert:
            continue
        key = (cert["year"], cert["agent_inn"], cert["cert_date"])
        if key in seen:  # секции пакета бывают продублированы
            continue
        seen.add(key)
        incomes.append(cert)
    if incomes:
        years = ", ".join(sorted({str(c["year"]) for c in incomes}))
        yield {"log": f"Справки 2-НДФЛ: {len(incomes)} шт. за {years}", "ok": True}
    else:
        yield {"log": "Справок 2-НДФЛ не найдено", "warn": True}

    # ── Иное: задолженность, участие в ЮЛ, адм. правонарушения ──
    has_debt = None
    admin: list[dict] = []
    legal_entities: list[dict] = []
    for kind, sec in sections:
        body = "\n".join(sec)
        if kind == "tax_debt" and has_debt is None:
            if re.search(r"^не имеет$", body, re.M):
                has_debt = False
            elif re.search(r"^имеет$", body, re.M):
                has_debt = True
        elif kind == "admin":
            admin += _parse_admin(sec)
        elif kind == "legal_entities" and NO_DATA not in body:
            rest = "\n".join(sec[1:]).strip()
            if rest:
                legal_entities.append({"title": "Участие в юридических лицах", "details": rest})
    if has_debt is not None:
        yield {"log": ("Неисполненная обязанность по налогам: ЕСТЬ" if has_debt
                       else "Неисполненной обязанности по налогам нет"),
               "warn": bool(has_debt)}
    if admin:
        yield {"log": f"Административные правонарушения: {len(admin)}", "warn": True}
    if legal_entities:
        yield {"log": "Найдено участие в юридических лицах", "warn": True}

    yield {"result": {
        **head,
        "has_tax_debt": has_debt,
        "accounts": accounts,
        "incomes": incomes,
        "realty": realty,
        "land": land,
        "vehicles": vehicles,
        "admin": admin,
        "legal_entities": legal_entities,
    }}


def parse_stream(data: bytes):
    """Генератор шагов парсинга PDF: {"log": …} …, в конце {"result": {…}}.

    Шаги уходят в UI живым логом (см. views.assets_parse).
    """
    import pdfplumber

    yield {"log": "Открываю PDF…"}
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise FnsParseError(f"Не удалось прочитать PDF: {exc}") from exc

    with pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
        yield {"log": f"Страниц в документе: {len(pages)}"}
        yield from analyze_stream(
            "\n".join(pages),
            objects_getter=lambda: _parse_object_tables(pdf),
            accounts_getter=lambda: _parse_account_tables(pdf),
        )


def parse(data: bytes) -> dict:
    """Синхронный разбор PDF (management-команда). Логи отбрасываются."""
    return _drain(parse_stream(data))


def parse_text(text: str) -> dict:
    """Синхронный разбор текстового слоя (тесты на фикстурах)."""
    return _drain(analyze_stream(text))


def _drain(events) -> dict:
    result = None
    for event in events:
        if "result" in event:
            result = event["result"]
    if result is None:
        raise FnsParseError("Парсер не вернул результат")
    return result
