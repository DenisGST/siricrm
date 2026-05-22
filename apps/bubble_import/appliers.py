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
    """Ссылку на просмотр Google Drive → прямую download-ссылку."""
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
    if "drive.google.com" in url:
        return _gdrive_direct_url(url)
    return url


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

    resp = requests.get(real_url, timeout=60, allow_redirects=True)
    resp.raise_for_status()
    content_type = (resp.headers.get("Content-Type") or "").lower()
    data = resp.content

    # Google Drive на больших/непубличных файлах отдаёт HTML-страницу.
    if "drive.google.com" in real_url and content_type.startswith("text/html"):
        raise RuntimeError(
            "Google Drive вернул HTML — файл недоступен по ссылке "
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
    raw_tel = rec.value("tel")

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
            else:
                rec.status = "error"
                rec.error = (
                    f"Возможный дубль по телефону +{phone}: уже есть клиент "
                    f"«{dup}» ({dup.id}), но ФИО непохожи (совпадение "
                    f"{sim:.0%}). Возможно, разные люди с одним номером — "
                    f"проверьте вручную."
                )
                rec.imported_at = None
                rec.save(update_fields=["status", "error", "imported_at"])
                return rec.status

    if client is None:
        client = Client(bubble_id=bid, **fields)
    elif merged_existing:
        # Обогащение существующего клиента: заполняем только пустые поля.
        _enrich_client(client, fields)
        if not client.bubble_id:
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

def apply_projectbfl(rec: BubbleRecord) -> str:
    """Перенести ProjectBFL в Service. Клиент (dolgnik) должен быть импортирован."""
    from apps.crm.models import Service

    v = rec.value
    bid = rec.bubble_id

    client = Client.objects.filter(bubble_id=v("dolgnik")).first()
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

    phone = normalize_phone(v("NumberTel"))
    client = Client.objects.filter(whatsapp_phone=phone).first() if phone else None
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
        # медиа: body — прямая ссылка на файл (Wasabi S3), не чистим
        content = strip_bbcode(caption)
        if body.startswith("http") or body.startswith("//"):
            stored = download_to_storedfile(
                body, f"wa_{rec.bubble_id}", f"wamedia_{rec.bubble_id}"[:64],
            )
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

    rec.status = "imported"
    rec.target_type = "Message"
    rec.target_id = str(msg.id)
    rec.error = ""
    rec.imported_at = timezone.now()
    rec.save(update_fields=["status", "target_type", "target_id", "error", "imported_at"])
    return rec.status


# ─── Files → StoredFile ────────────────────────────────────

def apply_files(rec: BubbleRecord) -> str:
    """Скачать файл Bubble в наш S3. Привязать к клиенту услуги, если есть."""
    from apps.crm.models import Service
    from apps.files.models import ClientFolder, ClientFile
    from apps.files.folder_utils import get_or_create_root

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
        client = service.client
        root = get_or_create_root(client)
        folder, _ = ClientFolder.objects.get_or_create(
            client=client, slug="bubble_import",
            defaults={"parent": root, "name": "Импорт из Bubble", "order": 9},
        )
        directory = clean_str(v("directory"))
        display_name = (f"{directory}/{filename}" if directory else filename)[:255]
        ClientFile.objects.get_or_create(
            stored_file=stored, folder=folder,
            defaults={
                "name": display_name,
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
    """Найти существующего Employee по фамилии (+ имени/инициалу)."""
    from apps.core.models import Employee
    if not last:
        return None
    cands = Employee.objects.filter(
        user__last_name__iexact=last,
    ).select_related("user")
    for e in cands:
        ef = (e.user.first_name or "").strip()
        if not first or not ef:
            return e
        if ef.lower() == first.lower() or ef[0].lower() == first[0].lower():
            return e
    return None


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


# Реестр applier'ов по типу сущности.
APPLIERS = {
    "Man": apply_man,
    "ProjectBFL": apply_projectbfl,
    "Money": apply_money,
    "MessageWSP": apply_messagewsp,
    "Files": apply_files,
    "User": apply_user,
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
