"""Telegram-бот для приёма заявок с лендингов через Bot API.

Бот @Sirius_system_bot добавлен админом в канал, куда лендинги шлют
заявки в фиксированном формате (см. _parse_lead). Telegram POST'ит
update'ы на наш webhook → парсим → создаём Client + Service + ставим
лид в «Мой канбан» сотрудникам с галкой accept_telegram_leads.

Если получателей с галкой нет — fallback на конкретного РОПа
(Власов Евгений, см. _fallback_employees).
"""
import json
import logging
import re

from decouple import config
from django.db import transaction
from django.http import JsonResponse, HttpResponseForbidden
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.crm.lead_routing import route_new_lead
from apps.crm.models import Client, ClientEvent
from apps.crm.phone_utils import add_client_phone, find_client_by_phone

logger = logging.getLogger("telegram_leads")

# Конфиг
BOT_TOKEN = config("TELEGRAM_BOT_TOKEN", default="")
LEADS_CHANNEL_ID = config("TELEGRAM_LEADS_CHANNEL_ID", default="", cast=str)
WEBHOOK_SECRET = config("TELEGRAM_LEADS_WEBHOOK_SECRET", default="")

# Формат заявки с лендинга — основные поля.
_PHONE_RE = re.compile(r"Телефон:\s*([+\d\s()\-]+)")
_NAME_RE = re.compile(r"Имя:\s*([^\n]+)")
_FORM_RE = re.compile(r"Название формы:\s*([^\n]+)")
_NUMBER_RE = re.compile(r"Новая заявка №\s*(\d+)")
_PAGE_RE = re.compile(r"со страницы\s+(\S+)")
_LINK_RE = re.compile(r"Просмотр заявки\s*\((https?://[^)\s]+)")


# ─── парсинг ───────────────────────────────────────────────


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits if len(digits) == 11 and digits.startswith("7") else ""


def _extract_answers(text: str) -> list[tuple[str, str]]:
    """Из тела заявки вытащить пользовательские ответы по форме как
    список (вопрос, ответ). Берём всё между «Данные формы:» и
    «персональные данные:» (или концом текста), пропускаем уже
    распарсенные «Имя:»/«Телефон:» и убираем HTML-теги из ответов."""
    body = text
    m = re.search(r"Данные формы:\s*\n", body)
    if m:
        body = body[m.end():]
    cut = re.search(r"(?im)^\s*(персональные\s+данные|просмотр\s+заявки)\b", body)
    if cut:
        body = body[:cut.start()]

    pairs: list[tuple[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        q, _, a = line.partition(":")
        q = q.strip()
        a = re.sub(r"<[^>]+>", "", a).strip()
        if not q or not a:
            continue
        if q.lower() in {"имя", "телефон"}:
            continue
        pairs.append((q, a))
    return pairs


def _parse_lead(text: str) -> dict | None:
    """Превратить текст сообщения с лендинга в dict полей. None если формат
    не похож на заявку."""
    if not text or "Новая заявка" not in text:
        return None
    name = _NAME_RE.search(text)
    phone_raw = _PHONE_RE.search(text)
    number = _NUMBER_RE.search(text)
    form = _FORM_RE.search(text)
    page = _PAGE_RE.search(text)
    link = _LINK_RE.search(text)
    phone = _normalize_phone(phone_raw.group(1) if phone_raw else "")
    return {
        "number": number.group(1).strip() if number else "",
        "form": form.group(1).strip() if form else "",
        "page": page.group(1).strip() if page else "",
        "name": name.group(1).strip() if name else "",
        "phone": phone,
        "link": link.group(1).strip() if link else "",
        "answers": _extract_answers(text),
        "raw": text,
    }


# «Лиды из Telegram», fallback на Власова — теперь в apps.crm.lead_routing.


# ─── создание лида ─────────────────────────────────────────


def _ensure_leads_emp_status(employee):
    # Тонкая обёртка для совместимости (используется в core/views.py быстрым
    # toggle'ом галки «Лиды TG»). Логика — в lead_routing.
    from apps.crm.lead_routing import ensure_lead_employee_status
    return ensure_lead_employee_status(employee)


@transaction.atomic
def create_lead_from_parsed(data: dict) -> Client:
    """Из распарсенного dict — создать (или найти) клиента + услугу +
    закрепить за получателями. Возвращает Client."""
    phone = data.get("phone") or ""
    name = data.get("name") or "Лид с лендинга"
    page = data.get("page") or ""
    link = data.get("link") or ""
    number = data.get("number") or ""
    form = data.get("form") or ""
    answers = data.get("answers") or []

    # Ответы из формы — пишутся в событие clientEvent (там их и смотрит юрист).
    answers_text = "\n".join(f"• {q}: {a}" for q, a in answers)

    notes_block = [
        f"Заявка с лендинга №{number} · форма «{form}»",
        f"Страница: {page}" if page else "",
        f"FlexBe: {link}" if link else "",
    ]
    if answers_text:
        notes_block += ["", "Ответы из формы:", answers_text]
    notes_text = "\n".join(x for x in notes_block if x)

    # Дедуп по телефону: если клиент уже есть — только событие.
    existing = find_client_by_phone(phone) if phone else None
    if existing is not None:
        desc = f"Повторный лид с лендинга (заявка №{number}, форма «{form}»)"
        if answers_text:
            desc += "\n\nОтветы из формы:\n" + answers_text
        ClientEvent.objects.create(
            client=existing, event_type="lead_received", employee=None,
            description=desc,
        )
        logger.info("lead %s: дубль по телефону, клиент %s", number, existing.id)
        return existing

    client = Client.objects.create(
        first_name=name[:255] or "Лид",
        phone=("+" + phone) if phone else "",
        whatsapp_phone=phone or None,
        status="lead",
        referral_source=(f"Лендинг: {page}" if page else "Лендинг")[:255],
        notes=notes_text,
        last_message_at=timezone.now(),
    )
    if phone:
        add_client_phone(client, phone, "primary")
        add_client_phone(client, phone, "whatsapp")
    logger.info("lead %s: создан клиент %s (%s)", number, client.id, name)

    source_label = f"Лендинг «{page}» · заявка №{number} · форма «{form}»"
    desc = (
        f"Новый лид с лендинга «{page}» (заявка №{number}, форма «{form}»)."
    )
    if answers_text:
        desc += "\n\nОтветы из формы:\n" + answers_text
    route_new_lead(client, source_label=source_label, event_description=desc)
    return client


# ─── webhook ──────────────────────────────────────────────


@csrf_exempt
def leads_webhook(request, secret: str = ""):
    """POST от Telegram Bot API. Принимает channel_post с заявкой."""
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return HttpResponseForbidden("bad secret")

    try:
        update = json.loads(request.body.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=200)

    logger.info("telegram-leads update: %s", str(update)[:500])

    # Берём channel_post или edited_channel_post или обычное message.
    msg = (
        update.get("channel_post")
        or update.get("edited_channel_post")
        or update.get("message")
    )
    if not msg:
        return JsonResponse({"ok": True, "skip": "no_message"})

    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if LEADS_CHANNEL_ID and chat_id != LEADS_CHANNEL_ID:
        logger.info("telegram-leads: пропущен chat_id=%s (ожидался %s)",
                    chat_id, LEADS_CHANNEL_ID)
        return JsonResponse({"ok": True, "skip": "wrong_chat"})

    text = msg.get("text") or msg.get("caption") or ""
    data = _parse_lead(text)
    if not data:
        return JsonResponse({"ok": True, "skip": "not_a_lead"})

    try:
        client = create_lead_from_parsed(data)
    except Exception as e:  # noqa: BLE001
        logger.exception("telegram-leads: ошибка создания лида: %s", e)
        return JsonResponse({"ok": False, "error": str(e)}, status=200)

    return JsonResponse({"ok": True, "client_id": str(client.id)})
