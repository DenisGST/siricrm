"""APPLY-логика: одобренные BubbleRecord → продакшн-модели SiriCRM.

Этап B3 — только Man → Client. ProjectBFL/Money/MessageWSP/Files — на B4+.

Идемпотентность: повторный apply находит Client по bubble_id и обновляет.
Дедупликация: перед созданием нового клиента ищем совпадение по телефону —
если нашли чужого клиента, ставим запись в статус error (оператор решит).
"""
import logging
import re
from difflib import SequenceMatcher

import requests
from django.db.models import Q
from django.utils import timezone

from apps.crm.models import Client, ClientNameHistory, ClientEvent

from .extractors import (
    clean_str, first_nonempty, normalize_phone,
    gender_from_bubble, parse_bubble_date, parse_bubble_dt,
    parse_decimal, parse_int, money_kind, strip_bbcode,
    parse_fio, map_bubble_role,
)
from .models import BubbleRecord
from . import resolvers

logger = logging.getLogger("bubble_import")


# ─── Скачивание файлов в наш S3 ────────────────────────────

def _gdrive_direct_url(url: str) -> str:
    """Ссылку на Google Drive / Docs → прямую download-ссылку.

    Поддерживает 4 кейса:
      * drive.google.com/file/d/<ID>/...      → uc?export=download&id=<ID>
      * drive.google.com/...?id=<ID>          → uc?export=download&id=<ID>
      * docs.google.com/document/d/<ID>/...   → export?format=pdf
      * docs.google.com/spreadsheets/d/<ID>/  → export?format=xlsx
      * docs.google.com/presentation/d/<ID>/  → export?format=pdf
    """
    # Google Docs / Sheets / Slides — отдельные эндпоинты экспорта.
    m_doc = re.search(r"docs\.google\.com/(document|spreadsheets|presentation)/d/([^/]+)", url)
    if m_doc:
        kind, gid = m_doc.group(1), m_doc.group(2)
        fmt = {"document": "pdf", "spreadsheets": "xlsx", "presentation": "pdf"}.get(kind, "pdf")
        return f"https://docs.google.com/{kind}/d/{gid}/export?format={fmt}"
    # Drive file.
    m = re.search(r"/file/d/([^/]+)", url) or re.search(r"[?&]id=([^&]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


def _normalize_file_url(url: str) -> str:
    """Привести ссылку Bubble к скачиваемому виду."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if "drive.google.com" in url or "docs.google.com" in url:
        return _gdrive_direct_url(url)
    return url


def _gdrive_fetch_with_confirm(url: str, timeout: int = 60):
    """Скачать файл с Drive, обходя страницу подтверждения для больших файлов.

    Если первый GET вернул HTML с предупреждением — извлекаем confirm-токен
    из cookies / query-string-параметра и повторяем GET с ним.
    """
    s = requests.Session()
    r = s.get(url, timeout=timeout, allow_redirects=True)
    ct = (r.headers.get("Content-Type") or "").lower()
    if not ct.startswith("text/html"):
        return r
    # Большой файл → confirm-token. Может быть в cookies или в HTML form.
    confirm = ""
    for k, v in s.cookies.items():
        if k.startswith("download_warning"):
            confirm = v
            break
    if not confirm:
        m = re.search(r'name="confirm"\s+value="([^"]+)"', r.text)
        if m:
            confirm = m.group(1)
    if not confirm:
        # Не получилось извлечь — отдаём как есть, выше определят HTML.
        return r
    sep = "&" if "?" in url else "?"
    r2 = s.get(f"{url}{sep}confirm={confirm}", timeout=timeout, allow_redirects=True)
    return r2


def download_to_storedfile(url: str, filename: str, bubble_id: str | None = None):
    """Скачать файл по ссылке и положить в наш Beget S3 как StoredFile.

    Идемпотентно по bubble_id: если StoredFile уже есть — вернуть его.
    Бросает исключение при сетевой ошибке / недоступности (apply_record ловит).
    """
    from apps.files.models import StoredFile
    from apps.files.s3_utils import upload_file_to_s3

    if bubble_id:
        existing = StoredFile.objects.filter(bubble_id=bubble_id).first()
        if existing:
            return existing

    real_url = _normalize_file_url(url)
    if not real_url:
        raise ValueError("Пустая ссылка на файл")

    # Для drive.google.com — отдельная функция с обходом confirm-страницы
    # на больших файлах. Для остальных URL — обычный requests.get.
    if "drive.google.com" in real_url:
        resp = _gdrive_fetch_with_confirm(real_url, timeout=60)
    else:
        resp = requests.get(real_url, timeout=60, allow_redirects=True)
    resp.raise_for_status()
    content_type = (resp.headers.get("Content-Type") or "").lower()
    data = resp.content

    # Если после всех попыток всё ещё HTML — файл реально недоступен
    # (403/закрытый доступ/удалённый).
    if ("drive.google.com" in real_url or "docs.google.com" in real_url) and content_type.startswith("text/html"):
        raise RuntimeError(
            "Google Drive/Docs вернул HTML — файл недоступен по ссылке "
            "или требует подтверждения. Проверьте доступ «всем по ссылке»."
        )

    bucket, key = upload_file_to_s3(
        data, prefix="bubble/import", filename=filename or "file.bin",
        content_type=content_type or None,
    )
    return StoredFile.objects.create(
        bucket=bucket, key=key, filename=filename or key,
        content_type=content_type, size=len(data), bubble_id=bubble_id,
    )


def _man_fields(rec: BubbleRecord) -> dict:
    """Собрать поля Client из записи Man (с учётом overrides оператора)."""
    v = rec.value
    return {
        "first_name": (clean_str(v("fName")) or "Без имени")[:255],
        "last_name": clean_str(v("lName"))[:255],
        "patronymic": clean_str(v("mName"))[:255],
        "birth_date": parse_bubble_date(v("dateR")),
        "birth_place": clean_str(v("cityR"))[:500],
        "passport_series": first_nonempty(v("PaspSer"), v("PassSer"))[:4],
        "passport_number": first_nonempty(v("PaspNumb"), v("passNumb"))[:6],
        "passport_issued_by": clean_str(v("passOut"))[:500],
        "passport_issued_date": parse_bubble_date(v("passDate")),
        "inn": first_nonempty(v("inn"), v("INN"))[:12],
        "snils": clean_str(v("snils"))[:14],
        "email": clean_str(v("email"))[:254],
        "notes": strip_bbcode(v("notes")),
        "gender": gender_from_bubble(v("Пол")),
        "is_married": bool(v("isMarried")),
        "referral_source": strip_bbcode(v("From"))[:255],
    }


# Порог схожести ФИО: дубль по телефону + ФИО ≥ этого → тот же человек.
FIO_MATCH_THRESHOLD = 0.9


def _is_dummy_phone(raw_tel) -> bool:
    """True, если телефон — заглушка из нулей (7000000 / 0000000 и т.п.).

    Клиенты с таким телефоном не импортируются. Пустой телефон
    заглушкой НЕ считается — это просто клиент без номера.
    """
    digits = re.sub(r"\D", "", clean_str(raw_tel))
    if not digits:
        return False
    if set(digits) == {"0"}:
        return True
    if normalize_phone(raw_tel) == "70000000000":
        return True
    return False


def _ratio(a: str, b: str):
    """Схожесть двух строк 0..1; None если одна из строк пустая."""
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return None
    return SequenceMatcher(None, a, b).ratio()


def _fio_similarity(rec: BubbleRecord, client: Client) -> float:
    """Схожесть ФИО записи Bubble и существующего клиента.

    Сравниваем покомпонентно (фамилия / имя / отчество). Отчество, если
    у одной из сторон пустое, в расчёт не берём — Telegram-клиенты часто
    без отчества. Фамилия и имя обязаны присутствовать у обоих.
    """
    v = rec.value
    last = _ratio(clean_str(v("lName")), client.last_name)
    first = _ratio(clean_str(v("fName")), client.first_name)
    patr = _ratio(clean_str(v("mName")), client.patronymic)
    if last is None or first is None:
        return 0.0
    parts = [x for x in (last, first, patr) if x is not None]
    return sum(parts) / len(parts)


def _enrich_client(client: Client, fields: dict):
    """Дописать данные Bubble в существующего клиента — только в пустые поля.

    Уже заполненные значения не перезатираем (у клиента из Telegram/MAX
    данные могли быть подтверждены сотрудником).
    """
    for key, value in fields.items():
        if not value:
            continue  # из Bubble пусто — дописывать нечего
        current = getattr(client, key, None)
        if current in (None, "", False):
            setattr(client, key, value)


def _assign_unknown_responsible(client: Client):
    """Клиента в статусе «Неразобран» закрепить за ответственным (Власов Евгений)."""
    if client.status != "unknown":
        return
    from apps.core.models import Employee
    from apps.crm.models import ClientEmployee
    emp = Employee.objects.filter(
        user__last_name__iexact="Власов", user__first_name__iexact="Евгений",
    ).first()
    if emp:
        ClientEmployee.objects.get_or_create(client=client, employee=emp)


def _apply_name_history(client: Client, rec: BubbleRecord):
    """Прежние ФИО из fNameOld / lNameOld / mNameOld."""
    v = rec.value
    old_last = clean_str(v("lNameOld"))
    old_first = clean_str(v("fNameOld"))
    old_patr = clean_str(v("mNameOld"))
    if not (old_last or old_first or old_patr):
        return
    ClientNameHistory.objects.get_or_create(
        client=client,
        last_name=old_last[:255],
        first_name=old_first[:255],
        patronymic=old_patr[:255],
        defaults={"note": "Импортировано из Bubble"},
    )


def apply_man(rec: BubbleRecord) -> str:
    """Перенести одну запись Man в Client. Возвращает итоговый статус."""
    bid = rec.bubble_id
    fields = _man_fields(rec)
    # phone_override — оператор подменил номер в overrides (например, для
    # семейного дубля, когда у двух людей реально один общий номер: одному
    # ставим fake-номер 7999990000X, чтобы не падал unique-constraint).
    phone_override = clean_str(rec.value("phone_override"))
    raw_tel = phone_override or rec.value("tel")

    # Телефон-заглушка из нулей → клиента не импортируем.
    if _is_dummy_phone(raw_tel):
        rec.status = "skipped"
        rec.error = "Телефон-заглушка (нули) — клиент не импортируется"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    phone = normalize_phone(raw_tel)

    client = Client.objects.filter(bubble_id=bid).first()
    existed_by_bubble = client is not None  # повторный импорт этой же записи
    merged_existing = False
    overwritten_dup = False
    # Оператор может явно отметить запись для перезаписи существующего
    # дубля по телефону (для случаев, когда в Bubble клиент задвоен и
    # эта запись — правильная). Флаг ставится из UI в overrides.
    force_overwrite = bool(rec.value("overwrite_dup"))
    # merge_into_client_id — оператор указал конкретного клиента для слияния
    # (когда автоматическая FIO-проверка не сработала, но мы знаем, что это
    # один и тот же человек). Принудительное merged_existing.
    forced_merge_id = clean_str(rec.value("merge_into_client_id"))
    # force_rename_existing — при merged_existing перезаписать ФИО клиента
    # данными из Bubble (для случаев «Объединить в X», когда существующий
    # клиент был «Неизвестно 3398», а Bubble прислал полное ФИО).
    force_rename = bool(rec.value("force_rename_existing"))

    if client is None and forced_merge_id:
        forced = Client.objects.filter(pk=forced_merge_id).first()
        if not forced:
            rec.status = "error"
            rec.error = (
                f"merge_into_client_id={forced_merge_id} — клиент не найден"
            )
            rec.imported_at = None
            rec.save(update_fields=["status", "error", "imported_at"])
            return rec.status
        client = forced
        merged_existing = True

    # Новый клиент — проверка на дубль по телефону.
    if client is None and phone:
        dup = Client.objects.filter(
            Q(whatsapp_phone=phone) | Q(phone="+" + phone)
        ).first()
        if dup:
            sim = _fio_similarity(rec, dup)
            if sim >= FIO_MATCH_THRESHOLD:
                # Тот же человек — дописываем данные Bubble в существующего.
                client = dup
                merged_existing = True
            elif force_overwrite:
                # Оператор подтвердил, что это тот же клиент — перезаписываем.
                client = dup
                overwritten_dup = True
            else:
                rec.status = "error"
                rec.error = (
                    f"Возможный дубль по телефону +{phone}: уже есть клиент "
                    f"«{dup}» ({dup.id}), но ФИО непохожи (совпадение "
                    f"{sim:.0%}). Возможно, разные люди с одним номером — "
                    f"проверьте вручную. Если это тот же человек — отметьте "
                    f"чекбокс «Перезаписать существующего»."
                )
                rec.imported_at = None
                rec.save(update_fields=["status", "error", "imported_at"])
                return rec.status

    if client is None:
        client = Client(bubble_id=bid, **fields)
    elif merged_existing and force_rename:
        # «Объединить в X» — обновляем ФИО клиента из Bubble (полное ФИО
        # заменяет огрызок типа «Неизвестно 3398»). Прежнее ФИО — в историю.
        old_last = client.last_name or ""
        old_first = client.first_name or ""
        old_patr = client.patronymic or ""
        if old_last or old_first or old_patr:
            ClientNameHistory.objects.get_or_create(
                client=client,
                last_name=old_last[:255],
                first_name=old_first[:255],
                patronymic=old_patr[:255],
                defaults={"note": "Прежнее ФИО до объединения с Bubble"},
            )
        for k, val in fields.items():
            if val:  # не перетираем пустыми
                setattr(client, k, val)
        if not client.bubble_id:
            client.bubble_id = bid
    elif merged_existing:
        # Обогащение существующего клиента: заполняем только пустые поля.
        _enrich_client(client, fields)
        if not client.bubble_id:
            client.bubble_id = bid
    elif overwritten_dup:
        # Жёсткая перезапись существующего клиента данными из Bubble.
        # Старое ФИО сохраняем в историю, чтобы поиск по нему ещё работал.
        old_last = client.last_name or ""
        old_first = client.first_name or ""
        old_patr = client.patronymic or ""
        if old_last or old_first or old_patr:
            ClientNameHistory.objects.get_or_create(
                client=client,
                last_name=old_last[:255],
                first_name=old_first[:255],
                patronymic=old_patr[:255],
                defaults={"note": "Перезапись при импорте дубля из Bubble"},
            )
        for k, val in fields.items():
            setattr(client, k, val)
        client.bubble_id = bid
    else:
        # Повторный импорт записи (найден по bubble_id) — обновляем полностью.
        for k, val in fields.items():
            setattr(client, k, val)

    # Статус клиента — по статусу его услуги в Bubble. Существующим
    # (обогащаемым) клиентам статус не меняем — они живут своей жизнью.
    if not merged_existing:
        mapped_status = resolvers.resolve_client_status_by_man(bid)
        if mapped_status:
            client.status = mapped_status

    if phone:
        client.phone = "+" + phone
        # whatsapp_phone уникален — ставим только если номер свободен.
        wa_taken = (
            Client.objects.filter(whatsapp_phone=phone)
            .exclude(pk=client.pk).exists()
        )
        if not wa_taken:
            client.whatsapp_phone = phone

    client.save()

    if phone:
        # Записываем номер в ClientPhone как primary + whatsapp (idempotent).
        from apps.crm.phone_utils import add_client_phone
        add_client_phone(client, phone, purpose="primary")
        add_client_phone(client, phone, purpose="whatsapp")

    _apply_name_history(client, rec)
    _assign_unknown_responsible(client)

    # Событие в лог клиента — только при первичном импорте/обогащении,
    # повторный apply той же записи событие не дублирует.
    if not existed_by_bubble:
        if merged_existing:
            ClientEvent.objects.create(
                client=client, event_type="bubble_enriched", employee=None,
                description="Данные клиента дополнены при импорте из CRM на bubble.io",
            )
        else:
            ClientEvent.objects.create(
                client=client, event_type="bubble_imported", employee=None,
                description="Клиент импортирован из CRM на bubble.io",
            )

    rec.status = "imported"
    rec.target_type = "Client"
    rec.target_id = str(client.id)
    rec.error = ""
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


def link_spouses() -> int:
    """Связать супругов: Man.spouse (bubble_id) → Client.spouse FK.

    Запускается после apply пакета — оба супруга должны быть импортированы.
    Возвращает число проставленных связей.
    """
    linked = 0
    recs = BubbleRecord.objects.filter(
        entity="Man", status="imported",
    ).exclude(raw__spouse=None)
    by_bubble = {
        c.bubble_id: c
        for c in Client.objects.exclude(bubble_id=None)
    }
    for rec in recs:
        spouse_bid = (rec.raw or {}).get("spouse")
        client = by_bubble.get(rec.bubble_id)
        spouse = by_bubble.get(spouse_bid) if spouse_bid else None
        if client and spouse and client.spouse_id != spouse.id:
            client.spouse = spouse
            client.save(update_fields=["spouse"])
            linked += 1
    return linked


# ─── ProjectBFL → Service ──────────────────────────────────

def _assign_service_employees(service, rec: BubbleRecord):
    """Закрепить за услугой сотрудников из полей Manager / ROP / Jurist /
    Arbitragnik. Manager/ROP/Jurist — Bubble User; Arbitragnik — Bubble
    Arbitrs (резолвится по ФИО). Возвращает число закреплённых."""
    from apps.crm.models import ServiceEmployeeState

    v = rec.value
    emp_ids = set()

    for field in ("Manager", "ROP", "Jurist"):
        emp = resolvers.resolve_employee_by_user(v(field))
        if emp:
            emp_ids.add(emp.pk)

    arb_fio = resolvers.lookup("Arbitrs", v("Arbitragnik"), "FIO")
    if arb_fio:
        al, af, _ = parse_fio(arb_fio)
        arb = _find_employee_by_fio(al, af)
        if arb:
            emp_ids.add(arb.pk)

    for eid in emp_ids:
        ServiceEmployeeState.objects.get_or_create(service=service, employee_id=eid)
    return len(emp_ids)


def apply_projectbfl(rec: BubbleRecord) -> str:
    """Перенести ProjectBFL в Service. Клиент (dolgnik) должен быть импортирован."""
    from apps.crm.models import Service

    v = rec.value
    bid = rec.bubble_id

    dolgnik_bid = v("dolgnik")
    client = Client.objects.filter(bubble_id=dolgnik_bid).first()
    if client is None and dolgnik_bid:
        # Fallback: Man мог быть merge'нут с существующим клиентом — тогда
        # Client.bubble_id остался от первого исторического Man'а, а
        # BubbleRecord(Man).target_id указывает на нужный pk клиента.
        man_rec = BubbleRecord.objects.filter(
            entity="Man", bubble_id=dolgnik_bid, status="imported",
        ).first()
        if man_rec and man_rec.target_id:
            client = Client.objects.filter(pk=man_rec.target_id).first()
    if client is None:
        rec.status = "error"
        rec.error = (
            "Клиент (dolgnik) не импортирован. Сначала импортируйте клиентов."
        )
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    agent = Client.objects.filter(bubble_id=v("agent")).first() if v("agent") else None

    fields = {
        "client": client,
        "agent": agent,
        "name": resolvers.resolve_bfl_service_name(),
        "region": resolvers.resolve_region(v("regionPrj")),
        "date_dogovor": parse_bubble_date(v("DateDogovor")),
        "date_end": parse_bubble_date(v("dateEndPrj")),
        "numb_dogovor": clean_str(v("numbDogovor"))[:50],
        "contract_price": parse_decimal(v("SummaDogovor")) or None,
        "legal_services_amount": parse_decimal(v("SummaJuruslugiDogovor")),
        "doc_collection": parse_decimal(v("SummaSborDocDogovor")),
        "postal_costs": parse_decimal(v("SummaPostRashDogovor")),
        "state_duty": parse_decimal(v("SummGosPoshDogovor")),
        "fu_fee": parse_decimal(v("SummaVoznagragdenieDogovor")),
        "procedure_costs": parse_decimal(v("SummPublicDogovor")),
        "additional_costs": parse_decimal(v("summDopRashodDogovor")),
        "installment_months": parse_int(v("RassrochkaDogovor"), default=6),
    }

    service = Service.objects.filter(bubble_id=bid).first()
    if service is None:
        service = Service(bubble_id=bid, **fields)
    else:
        for k, val in fields.items():
            setattr(service, k, val)
    service.save()

    # Статус клиента определяется статусом услуги (statusPrj).
    new_status = resolvers.resolve_client_status(v("statusPrj"))
    if new_status and client.status != new_status:
        client.status = new_status
        client.save(update_fields=["status"])

    # Закрепить услугу за сотрудниками (Manager / ROP / Jurist / Arbitragnik).
    _assign_service_employees(service, rec)

    # Перенести WhatsApp-номер услуги (telWSP) в ClientPhone-алиасы клиента,
    # чтобы input WhatsApp-сообщений по нему находил клиента, даже если у
    # Client.whatsapp_phone уже другой основной номер.
    tel_wsp = normalize_phone(rec.raw.get("telWSP") if rec.raw else "")
    if tel_wsp:
        from apps.crm.phone_utils import add_client_phone
        add_client_phone(client, tel_wsp, purpose="whatsapp")

    rec.status = "imported"
    rec.target_type = "Service"
    rec.target_id = str(service.id)
    rec.error = ""
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


# ─── Money → Charge / Payment ──────────────────────────────

def apply_money(rec: BubbleRecord) -> str:
    """Перенести Money: accrual→Charge, debit→Payment(in), credit→Payment(out)."""
    from apps.crm.models import Service
    from apps.finance.models import Charge, Payment

    v = rec.value
    raw = rec.raw or {}
    bid = rec.bubble_id

    kind = money_kind(raw)
    if kind == "empty":
        rec.status = "skipped"
        rec.error = "Пустая запись Money (нет accrual/debit/credit)"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    project_bid = v("Project")
    service = Service.objects.filter(bubble_id=project_bid).first() if project_bid else None
    if service is None:
        rec.status = "error"
        rec.error = (
            "Нет связанной услуги: Project пуст или ProjectBFL не импортирован."
        )
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    client = service.client
    date = parse_bubble_date(v("date"))
    if date is None:
        dt = parse_bubble_dt(raw.get("Created Date"))
        date = dt.date() if dt else timezone.now().date()
    amount = parse_decimal(v(kind))
    name = clean_str(v("name"))[:255] or "Импорт из Bubble"
    comments = strip_bbcode(v("comments"))

    if kind == "accrual":
        paid = bool(v("Paid"))
        defaults = {
            "client": client, "service": service,
            "due_date": date, "title": name, "amount": amount,
            "status": "paid" if paid else "scheduled",
            "comments": comments,
        }
        obj, _ = Charge.objects.update_or_create(bubble_id=bid, defaults=defaults)
        target_type = "Charge"
    else:
        direction = "in" if kind == "debit" else "out"
        defaults = {
            "client": client, "service": service,
            "payment_date": date, "direction": direction,
            "payment_form": "cashless", "comments": comments,
        }
        if direction == "in":
            defaults["amount_in"] = amount
            defaults["income_type"] = resolvers.resolve_income_type(v("typeDebit"))
            defaults["incoming_account"] = resolvers.resolve_incoming_account(v("MoneySource"))
        else:
            defaults["amount_out"] = amount
            defaults["expense_type"] = resolvers.resolve_expense_type(v("typeCredit"))
            defaults["outgoing_account"] = resolvers.resolve_outgoing_account(v("MoneySource"))
        obj, _ = Payment.objects.update_or_create(bubble_id=bid, defaults=defaults)
        target_type = "Payment"

    rec.status = "imported"
    rec.target_type = target_type
    rec.target_id = str(obj.id)
    rec.error = ""
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


# ─── MessageWSP → Message ──────────────────────────────────

def _wa_client_phone(raw: dict) -> str:
    """Определить телефон клиента (собеседника) из записи MessageWSP.

    Нельзя брать NumberTel напрямую: у исходящих сообщений там НАШ номер,
    а не клиента. Телефон собеседника надёжнее всего в chatId / id —
    формат «<phone>@c.us». NumberTel — запасной вариант и только для
    входящих сообщений.
    """
    for key in ("chatId", "id"):
        m = re.search(r"(\d{10,15})@c\.us", clean_str(raw.get(key)))
        if m:
            return normalize_phone(m.group(1))
    if not bool(raw.get("fromMe")):
        return normalize_phone(raw.get("NumberTel"))
    return ""


_WA_TYPE_MAP = {
    "chat": "text", "image": "image", "video": "video",
    "audio": "audio", "ptt": "voice", "document": "document",
    "sticker": "image",
}


def apply_messagewsp(rec: BubbleRecord) -> str:
    """Перенести MessageWSP в Message. Клиент ищется по whatsapp_phone."""
    from apps.crm.models import Message

    v = rec.value
    raw = rec.raw or {}

    mtype = clean_str(v("type")).lower()
    body = clean_str(v("body"))
    caption = clean_str(v("caption"))
    if not mtype or (not body and not caption):
        rec.status = "skipped"
        rec.error = "Пустое сообщение (нет type/body/caption)"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    phone = _wa_client_phone(raw)
    client = Client.objects.filter(whatsapp_phone=phone).first() if phone else None
    if client is None and phone:
        # Fallback: telWSP из ProjectBFL → ClientPhone-алиас.
        from apps.crm.phone_utils import find_client_by_phone
        client = find_client_by_phone(phone)
    if client is None:
        rec.status = "error"
        rec.error = (
            f"Клиент с номером +{phone or '?'} не найден. "
            f"Сначала импортируйте клиентов."
        )
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    direction = "outgoing" if bool(v("fromMe")) else "incoming"
    created = parse_bubble_dt(v("Created Date"))
    message_type = _WA_TYPE_MAP.get(mtype, "text")

    stored = None
    if mtype == "chat":
        content = strip_bbcode(body)
    else:
        # медиа: body — прямая ссылка на файл (Wasabi S3 / bubble CDN)
        content = strip_bbcode(caption)
        if body.startswith("http") or body.startswith("//"):
            try:
                stored = download_to_storedfile(
                    body, f"wa_{rec.bubble_id}", f"wamedia_{rec.bubble_id}"[:64],
                )
            except Exception as e:  # noqa: BLE001
                # Недоступное медиа (403 bubble CDN и т.п.) не должно
                # ронять импорт самого сообщения.
                logger.warning("WA media %s недоступно: %s", rec.bubble_id, e)
                stored = None
                if not content:
                    content = "(медиа недоступно)"
        elif not content:
            content = strip_bbcode(body)

    reply_to = None
    qmid = clean_str(v("quotedMsgId"))
    if qmid:
        reply_to = Message.objects.filter(
            whatsapp_message_id=qmid, channel="whatsapp",
        ).first()

    defaults = {
        "client": client,
        "content": content,
        "direction": direction,
        "message_type": message_type,
        "channel": "whatsapp",
        "whatsapp_message_id": clean_str(v("id"))[:128],
        "reply_to": reply_to,
        "telegram_date": created or timezone.now(),
    }
    if stored:
        defaults["file"] = stored
        defaults["file_name"] = stored.filename

    msg, _ = Message.objects.update_or_create(bubble_id=rec.bubble_id, defaults=defaults)

    # Поднять клиента в списке чатов — иначе с импортированной перепиской
    # он не всплывёт (список сортируется по last_message_at).
    msg_dt = defaults["telegram_date"]
    if client.last_message_at is None or msg_dt > client.last_message_at:
        client.last_message_at = msg_dt
        client.save(update_fields=["last_message_at"])

    rec.status = "imported"
    rec.target_type = "Message"
    rec.target_id = str(msg.id)
    rec.error = ""
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


# ─── Files → StoredFile ────────────────────────────────────

def _bubble_folder_path(client, directory: str):
    """По пути Bubble («Процедура/Запросы») создать дерево папок в корне
    клиента и вернуть листовую папку. Пустой путь → корневая папка клиента.
    Существующие папки с тем же именем переиспользуются."""
    from apps.files.models import ClientFolder
    from apps.files.folder_utils import get_or_create_root

    parent = get_or_create_root(client)
    for part in (directory or "").split("/"):
        part = part.strip()
        if not part:
            continue
        parent, _ = ClientFolder.objects.get_or_create(
            client=client, parent=parent, name=part[:200],
            defaults={"order": 100},
        )
    return parent


def apply_files(rec: BubbleRecord) -> str:
    """Скачать файл Bubble в наш S3. Привязать к клиенту услуги, если есть."""
    from apps.crm.models import Service
    from apps.files.models import ClientFile

    v = rec.value
    link = clean_str(v("linkGDrive"))
    if not link:
        rec.status = "skipped"
        rec.error = "Пустая ссылка linkGDrive"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    # Старое хранилище Bubble S3 (appforest_uf, ~2020) отдаёт 403 —
    # такие файлы пропускаем, не тратя сетевой запрос.
    if "appforest" in link or "s3.amazonaws" in link:
        rec.status = "skipped"
        rec.error = "Старый файл Bubble S3 (appforest) — недоступен, пропущен"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    filename = clean_str(v("filename")) or f"file_{rec.bubble_id}"
    stored = download_to_storedfile(link, filename, rec.bubble_id)

    # Привязка к клиенту через услугу (projectBFL).
    service = None
    project_bid = v("projectBFL")
    if project_bid:
        service = Service.objects.filter(bubble_id=project_bid).first()
    if service is not None:
        # Раскладываем по дереву папок согласно полю directory Bubble.
        folder = _bubble_folder_path(service.client, clean_str(v("directory")))
        ClientFile.objects.get_or_create(
            stored_file=stored, folder=folder,
            defaults={
                "name": filename[:255],
                "size": stored.size or 0,
                "content_type": stored.content_type,
            },
        )

    rec.status = "imported"
    rec.target_type = "StoredFile"
    rec.target_id = str(stored.id)
    rec.error = ""
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


# ─── User → Employee ───────────────────────────────────────

def _find_employee_by_fio(last: str, first: str):
    """Найти существующего Employee по ФИО, устойчиво к опечаткам.

    Фамилия сравнивается нечётко (SequenceMatcher ≥ 0.85) — чтобы
    «Дмитриева» сматчилась с «Дмитириева». Имя, если известно у обоих,
    должно совпадать хотя бы по первой букве (отсев однофамильцев).
    """
    from apps.core.models import Employee
    if not last:
        return None
    last_l = last.lower().strip()
    first_l = (first or "").lower().strip()
    best, best_ratio = None, 0.0
    for e in Employee.objects.select_related("user"):
        el = (e.user.last_name or "").lower().strip()
        ef = (e.user.first_name or "").lower().strip()
        if not el:
            continue
        ratio = SequenceMatcher(None, last_l, el).ratio()
        if ratio < 0.85:
            continue
        if first_l and ef and first_l[0] != ef[0]:
            continue  # имена начинаются по-разному — разные люди
        if ratio > best_ratio:
            best_ratio, best = ratio, e
    return best


def apply_user(rec: BubbleRecord) -> str:
    """Перенести Bubble User в Employee. Действующих сопоставляем с
    существующими по ФИО, уволенных и новых — создаём."""
    from django.contrib.auth.models import User
    from apps.core.models import Employee

    v = rec.value
    bid = rec.bubble_id
    role_text = clean_str(v("role"))

    if role_text.lower() == "тестовый":
        rec.status = "skipped"
        rec.error = "Тестовый аккаунт — не импортируется"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    fio = clean_str(v("FIOLong")) or clean_str(v("UserName"))
    last, first, patr = parse_fio(fio)
    if not last:
        rec.status = "error"
        rec.error = "Не удалось определить ФИО (пустые FIOLong и UserName)"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    role = map_bubble_role(role_text)
    uvolen = bool(v("uvolen"))

    emp = Employee.objects.filter(bubble_id=bid).first()
    if emp is not None:
        note = f"Уже связан: {emp.user.get_full_name() or emp.user.username}"
    else:
        match = _find_employee_by_fio(last, first)
        if match is not None:
            emp = match
            # bubble_id хранит первую связанную запись; не перезатираем,
            # если у сотрудника несколько User-записей в Bubble.
            if not emp.bubble_id:
                emp.bubble_id = bid
                emp.save(update_fields=["bubble_id"])
            note = f"Сопоставлен с существующим: {emp.user.get_full_name()}"
        else:
            username = f"bubble_{bid}"
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={
                    "first_name": first[:150],
                    "last_name": last[:150],
                    "is_active": not uvolen,
                },
            )
            emp = Employee.objects.create(
                user=user, role=role, patronymic=patr[:255],
                is_active=not uvolen, bubble_id=bid,
            )
            note = f"Создан новый сотрудник (роль: {emp.get_role_display()})"

    rec.status = "imported"
    rec.target_type = "Employee"
    rec.target_id = str(emp.id)
    rec.error = note  # информативная пометка о результате сопоставления
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


# ─── Organization → LegalEntity ──────────────────────────────

def _normalize_inn_candidates(raw) -> list[str]:
    """Кандидаты валидного ИНН из «грязного» ввода Bubble.

    Возвращает список вариантов 10/12-значного ИНН для пробинга в
    LegalEntity / DaData. Для 11-значного — обе версии (срез первой и
    последней цифры), для 10/12 — сам ИНН.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) in (10, 12):
        return [digits]
    if len(digits) == 11:
        return [digits[1:], digits[:-1]]
    return []


def _normalize_inn(raw) -> str:
    """Возвращает первый кандидат (для случаев, когда нужен один ИНН)."""
    c = _normalize_inn_candidates(raw)
    return c[0] if c else ""


def apply_organization(rec: BubbleRecord) -> str:
    """Bubble Organization → LegalEntity.

    Pipeline:
      1. По bubble_id — повторный импорт той же записи.
      2. По ИНН — есть в нашем справочнике (>10 тыс. юрлиц уже залиты).
      3. DaData findById/party — если ИНН валидный, тянем реквизиты и
         создаём новый LegalEntity.
      4. DaData suggest/party — если ИНН нет/невалидный, fuzzy по
         shortOrgName/fullOrgName.
      5. Не нашли — error (юрист разрулит вручную).
    """
    from apps.crm.models import LegalEntity
    from apps.crm.dadata_legal import find_by_inn, search_by_name

    v = rec.value
    bid = rec.bubble_id
    inn_candidates = _normalize_inn_candidates(v("innOrg"))
    short_name = clean_str(v("shortOrgName"))
    full_name = clean_str(v("fullOrgName"))

    # 1. Повторный импорт по bubble_id
    le = LegalEntity.objects.filter(bubble_id=bid).first()
    matched_by = "bubble_id" if le else None
    inn = inn_candidates[0] if inn_candidates else ""

    # 2. По ИНН в нашем справочнике — пробуем все кандидаты
    if le is None and inn_candidates:
        for cand in inn_candidates:
            le = LegalEntity.objects.filter(inn=cand).first()
            if le:
                matched_by = "inn_local"
                inn = cand
                break

    dadata_payload = None
    # 3. DaData по ИНН — пробуем все кандидаты
    if le is None and inn_candidates:
        for cand in inn_candidates:
            dadata_payload = find_by_inn(cand)
            if dadata_payload:
                matched_by = "dadata_inn"
                inn = cand
                break

    # 4. DaData по имени
    if le is None and not dadata_payload:
        query = full_name or short_name
        if query:
            dadata_payload = search_by_name(query)
            if dadata_payload:
                matched_by = "dadata_name"
                inn = dadata_payload.get("inn") or inn

    # Создание или обогащение LegalEntity
    if le is None and dadata_payload is None:
        rec.status = "error"
        rec.error = (
            f"LegalEntity не найдено по ИНН={inn!r}, DaData тоже не "
            f"распознал. Нужно завести вручную: {short_name or full_name}"
        )
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    if le is None:
        # Создание нового из DaData payload — дополняем нашими адресными
        # полями из Bubble (Bubble хранит почтовый адрес, DaData — юр.).
        bubble_address = clean_str(v("adres"))
        le = LegalEntity.objects.create(
            bubble_id=bid,
            postal_address=bubble_address[:1000],
            **dadata_payload,
        )
    else:
        # Существующий — закрепляем bubble_id (если ещё нет), дозаполняем
        # пустые поля. Существующие значения не перезаписываем — справочник
        # может быть проверен оператором.
        changed = []
        if not le.bubble_id:
            le.bubble_id = bid
            changed.append("bubble_id")
        # postal_address из Bubble обычно полнее (с индексом)
        if not le.postal_address and v("adres"):
            le.postal_address = clean_str(v("adres"))[:1000]
            changed.append("postal_address")
        if dadata_payload:
            for k, val in dadata_payload.items():
                if val and not getattr(le, k, None):
                    setattr(le, k, val)
                    changed.append(k)
        if changed:
            le.save(update_fields=changed + ["updated_at"])

    rec.status = "imported"
    rec.target_type = "LegalEntity"
    rec.target_id = str(le.id)
    rec.error = f"Найдено через {matched_by}"
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


# ─── Kreditors → Kreditor ────────────────────────────────────

def apply_kreditor(rec: BubbleRecord) -> str:
    """Bubble Kreditors → Kreditor.

    Связи:
      - bfl → Service (с fallback через BubbleRecord(ProjectBFL).target_id
        для редких случаев, когда Service.bubble_id мог разойтись);
      - organization → LegalEntity (по bubble_id — должен быть уже
        импортирован applier'ом apply_organization).
    """
    from apps.crm.models import Service, LegalEntity, Kreditor

    v = rec.value
    bid = rec.bubble_id

    bfl_bid = v("bfl")
    service = Service.objects.filter(bubble_id=bfl_bid).first() if bfl_bid else None
    if service is None and bfl_bid:
        pf = BubbleRecord.objects.filter(
            entity="ProjectBFL", bubble_id=bfl_bid, status="imported",
        ).first()
        if pf and pf.target_id:
            service = Service.objects.filter(pk=pf.target_id).first()
    if service is None:
        rec.status = "error"
        rec.error = "Услуга (bfl) не импортирована. Сначала ProjectBFL."
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    org_bid = v("organization")
    legal_entity = None
    if org_bid:
        legal_entity = LegalEntity.objects.filter(bubble_id=org_bid).first()
        if legal_entity is None:
            # Может быть импортированный, но bubble_id не привязан (matched
            # по ИНН) — спросим через BubbleRecord(Organization).target_id.
            or_rec = BubbleRecord.objects.filter(
                entity="Organization", bubble_id=org_bid, status="imported",
            ).first()
            if or_rec and or_rec.target_id:
                legal_entity = LegalEntity.objects.filter(pk=or_rec.target_id).first()

    sum_all = parse_decimal(v("summAll"))
    debt_basis = strip_bbcode(v("debtBasis"))

    kreditor, _ = Kreditor.objects.update_or_create(
        bubble_id=bid,
        defaults={
            "service": service,
            "legal_entity": legal_entity,
            "name_manual": "" if legal_entity else (
                clean_str(v("organization")) or "Без названия"
            )[:500],
            "source": "anketa",
            "debt_basis": debt_basis,
            "sum_anketa": sum_all,
        },
    )

    rec.status = "imported"
    rec.target_type = "Kreditor"
    rec.target_id = str(kreditor.id)
    rec.error = ""
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


# ─── PropetyAnketa → Answer (property_assets в анкете БФЛ) ──────────────

# Паттерны «нет имущества» — клиент написал в анкете «нет / только прописка»
# и т.п. Такие записи не превращаем в entry, а пропускаем со skipped.
_NO_ASSET_PATTERNS = [
    r"^нет$", r"^—$", r"^-$", r"^нет ничего$",
    r"^123$", r"^321$", r"^ничего$",
    r"только\s+прописка",
    r"ничего\s+нет",
    r"нет\s+ничего",
]
_NO_ASSET_RE = re.compile("|".join(_NO_ASSET_PATTERNS), re.IGNORECASE)

# Классификация name → asset_type. Порядок проверки: realestate, vehicle, other.
_REALESTATE_RE = re.compile(
    r"квартир|дом\b|дома\b|дому|комнат|дач|гараж|земельн|земля|"
    r"недвиж|недвижим|участ|дду|помещен|здан|нежил|жилое|жилого|"
    r"долю?\s+в|часть\s+в|часть\s+дом|часть\s+квартир|долев",
    re.IGNORECASE,
)
_VEHICLE_RE = re.compile(
    r"авто|машин|ваз|лад[ау]|kia|hyundai|toyota|bmw|mercedes|renault|"
    r"прицеп|мотоцикл|пежо|peugeot|datsun|датсун|нива|лодк|катер|"
    r"яхт|тягач|трактор|скутер|мопед|kamaz|камаз|газ\s|уаз|"
    r"sportage|forester|focus|priora|granta",
    re.IGNORECASE,
)


def _classify_asset_type(name: str) -> str:
    n = (name or "").strip().lower()
    if _REALESTATE_RE.search(n):
        return "Недвижимость"
    if _VEHICLE_RE.search(n):
        return "Транспорт"
    return "Иное"


def _yesno(v) -> str:
    """Bubble bool → «Да» / «Нет»; пустое значение → «»."""
    if v is True:
        return "Да"
    if v is False:
        return "Нет"
    return ""


def apply_propety_anketa(rec: BubbleRecord) -> str:
    """Bubble PropetyAnketa → запись в Answer(question_type='property_assets').

    Каждая Bubble-запись → entry в JSON-массиве `value.entries`. Внутри
    entry храним `_bubble_id` для идемпотентности (повторный apply
    обновит существующий entry, а не задублирует).
    """
    from apps.crm.models import Service
    from apps.questionnaire.models import (
        QuestionnaireTemplate, QuestionnaireResponse, Question, Answer,
    )

    v = rec.value
    bid = rec.bubble_id
    name = clean_str(v("NameProperty"))

    # Пропускаем «нет / только прописка» — это не имущество, а ответ
    # клиента что имущества у него нет.
    if not name or _NO_ASSET_RE.search(name):
        rec.status = "skipped"
        rec.error = f"NameProperty={name!r} — не имущество, а ответ «нет»"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    project_bid = v("ProjectBFL")
    if not project_bid:
        rec.status = "skipped"
        rec.error = "Запись без ProjectBFL — невозможно привязать к услуге"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    service = Service.objects.filter(bubble_id=project_bid).first()
    if service is None:
        # Fallback: ProjectBFL мог импортироваться, но Service.bubble_id
        # быть не равен (хотя для services такого не бывает — service.bubble_id
        # всегда ставится в apply_projectbfl). На всякий — проверим target_id.
        pf = BubbleRecord.objects.filter(
            entity="ProjectBFL", bubble_id=project_bid, status="imported",
        ).first()
        if pf and pf.target_id:
            service = Service.objects.filter(pk=pf.target_id).first()
    if service is None:
        rec.status = "error"
        rec.error = "Услуга (ProjectBFL) не импортирована"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    template = QuestionnaireTemplate.objects.filter(
        service_name=service.name,
    ).first()
    if template is None:
        rec.status = "error"
        rec.error = (
            f"Нет QuestionnaireTemplate для услуги {service.name} "
            "(анкета не создана)"
        )
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    q = Question.objects.filter(
        page__template=template, question_type="property_assets",
    ).first()
    if q is None:
        rec.status = "error"
        rec.error = "В шаблоне анкеты нет вопроса property_assets"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status

    response, _ = QuestionnaireResponse.objects.get_or_create(
        service=service, template=template,
        defaults={"is_complete": False, "current_page": 0, "filled_by": None},
    )

    # In_marriage — «Да» если приобретено в браке ИЛИ оформлено на супруга
    in_marriage = "Да" if (v("FromBrak") or v("InSuprug")) else ""

    summa = v("Summa")
    if summa is not None and summa != "":
        try:
            summa_str = str(int(summa)) if float(summa) == int(float(summa)) else str(summa)
        except (TypeError, ValueError):
            summa_str = str(summa)
    else:
        summa_str = ""

    entry = {
        "_bubble_id":   bid,
        "asset_type":   _classify_asset_type(name),
        "name":         name[:500],
        "acquisition":  clean_str(v("Base"))[:500],
        "value":        summa_str,
        "pledged":      "",  # в PropetyAnketa нет поля «в залоге»
        "in_marriage":  in_marriage,
        "auction":      _yesno(v("Realizacia")),
        "comment":      strip_bbcode(v("comments"))[:1000],
    }

    answer, _ = Answer.objects.get_or_create(
        response=response, question=q, group_index=0,
        defaults={"value": {"has_assets": "yes", "entries": [entry]}},
    )
    value = answer.value or {"has_assets": "yes", "entries": []}
    value["has_assets"] = "yes"
    entries = value.get("entries", [])

    # Идемпотентность: если entry с этим _bubble_id уже есть — обновляем
    replaced = False
    for i, e in enumerate(entries):
        if e.get("_bubble_id") == bid:
            entries[i] = entry
            replaced = True
            break
    if not replaced:
        entries.append(entry)

    value["entries"] = entries
    answer.value = value
    answer.save(update_fields=["value"])

    rec.status = "imported"
    rec.target_type = "Answer"
    rec.target_id = str(answer.id)
    rec.error = ""
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


# Реестр applier'ов по типу сущности.
APPLIERS = {
    "Man": apply_man,
    "ProjectBFL": apply_projectbfl,
    "Money": apply_money,
    "MessageWSP": apply_messagewsp,
    "Files": apply_files,
    "User": apply_user,
    "Organization": apply_organization,
    "Kreditors": apply_kreditor,
    "PropetyAnketa": apply_propety_anketa,
}


def apply_record(rec: BubbleRecord) -> str:
    """Применить одну запись. Ошибки ловятся и пишутся в rec.error."""
    fn = APPLIERS.get(rec.entity)
    if fn is None:
        rec.status = "error"
        rec.error = f"Нет applier для сущности {rec.entity}"
        rec.save(update_fields=["status", "error"])
        return rec.status
    try:
        return fn(rec)
    except Exception as e:  # noqa: BLE001 — staging, ошибку показываем оператору
        logger.exception("apply %s/%s failed", rec.entity, rec.bubble_id)
        rec.status = "error"
        rec.error = f"{type(e).__name__}: {e}"
        rec.imported_at = None
        rec.save(update_fields=["status", "error", "imported_at"])
        return rec.status


def apply_approved(entity: str) -> dict:
    """Применить все одобренные ещё не импортированные записи сущности."""
    qs = BubbleRecord.objects.filter(
        entity=entity, approved=True,
    ).exclude(status="imported")
    imported = errors = 0
    for rec in qs:
        st = apply_record(rec)
        if st == "imported":
            imported += 1
        else:
            errors += 1
    extra = {}
    if entity == "Man":
        extra["spouses_linked"] = link_spouses()
    return {"imported": imported, "errors": errors, **extra}
