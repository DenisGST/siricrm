"""WOPI-хост для онлайн-правки документов запросов в Collabora Online.

Как это работает
────────────────
1. Юрист жмёт «Открыть в редакторе» → `editor_frame` отдаёт страницу-обёртку
   с самоотправляющейся формой на Collabora (urlsrc из её discovery).
2. Collabora получает `WOPISrc` — адрес НАШЕГО документа — и ходит по нему
   серверными запросами из соседнего контейнера: `check_file_info` (метаданные),
   `file_contents` GET (забрать .docx) и POST (вернуть изменённый).
3. Каждое сохранение перезаписывает ТОТ ЖЕ объект в S3, а PDF пересобирается
   отложенной celery-задачей.

🛑 У Collabora нет и не может быть сессии Django — она отдельный контейнер.
Единственная авторизация её запросов — подписанный `access_token`, который мы
выдаём юристу на конкретный запрос при открытии редактора. Поэтому вьюхи ниже
НЕ имеют `login_required`, но каждая обязана проверить токен.

🛑 Collabora ходит на внутренний адрес `http://web:8000` (см. WOPI_INTERNAL_URL),
а не на публичный домен: так надёжнее, чем разворот трафика через внешний IP.
Для этого `web` добавлен в ALLOWED_HOSTS, а путь `/wopi/` — в
SECURE_REDIRECT_EXEMPT (иначе SECURE_SSL_REDIRECT отдал бы ей 301 на https).
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.files.s3_utils import download_file_from_s3, upload_file_to_s3_key

from .models import Request as RequestModel

log = logging.getLogger(__name__)

SALT = "procedure.wopi.request-document"
# Сколько живёт токен доступа к документу. Правка письма — это минуты, но юрист
# может оставить вкладку открытой на весь рабочий день, поэтому 12 часов.
TOKEN_TTL = 12 * 60 * 60
DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ── Токен доступа ───────────────────────────────────────────────────────────

def issue_token(req, employee) -> str:
    """Подписанный токен на правку одного документа одним сотрудником."""
    return dumps(
        {"r": str(req.pk), "e": (str(employee.pk) if employee else ""),
         "n": _employee_name(employee)},
        salt=SALT,
    )


def _employee_name(employee) -> str:
    if not employee:
        return "Сотрудник"
    user = getattr(employee, "user", None)
    if user is None:
        return str(employee)
    return user.get_full_name() or user.username


def _payload(request):
    """Разобрать access_token из запроса Collabora. Ошибка → None."""
    token = request.GET.get("access_token") or ""
    if not token:
        return None
    try:
        return loads(token, salt=SALT, max_age=TOKEN_TTL)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        # Мусор вместо токена (обрезанный base64, кириллица) даёт не BadSignature,
        # а binascii/ValueError — это тоже «доступ запрещён», а не 500-я.
        return None


def _req_from(request, req_id):
    """Запрос по id + проверка, что токен выдан именно на него."""
    data = _payload(request)
    if not data or data.get("r") != str(req_id):
        return None, None
    req = (RequestModel.objects
           .select_related("document_docx", "case__service__client")
           .filter(pk=req_id).first())
    if req is None or not req.document_docx_id:
        return None, None
    return req, data


# ── Discovery: где живёт сам редактор ───────────────────────────────────────

_DISCOVERY_CACHE_KEY = "procedure:wopi:urlsrc:docx"


def editor_urlsrc() -> str:
    """`urlsrc` редактора для .docx из discovery Collabora (кэш в Redis).

    🛑 В URL зашит хеш версии сборки (`/browser/<hash>/cool.html`) — после
    обновления образа он меняется, поэтому кэшируем ненадолго и никогда не
    зашиваем путь в код.
    """
    cached = cache.get(_DISCOVERY_CACHE_KEY)
    if cached:
        return cached
    url = f"{settings.COLLABORA_INTERNAL_URL.rstrip('/')}/hosting/discovery"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    xml = resp.text
    # Ищем действие edit для docx; фолбэк — любой cool.html.
    m = re.search(
        r'<app\s+name="[^"]*wordprocessingml\.document"[^>]*>\s*'
        r'<action[^>]*urlsrc="([^"]+)"',
        xml,
    )
    if not m:
        m = re.search(r'urlsrc="([^"]*cool\.html\?[^"]*)"', xml)
    if not m:
        raise RuntimeError("Collabora discovery: не найден urlsrc редактора")
    src = m.group(1)
    # Discovery отдаёт адрес по Host, с которым мы к ней постучались (имя
    # контейнера). Браузеру нужен публичный домен — подменяем на наш.
    src = re.sub(r"^https?://[^/]+", settings.COLLABORA_PUBLIC_URL.rstrip("/"), src)
    cache.set(_DISCOVERY_CACHE_KEY, src, 60 * 30)
    return src


# ── WOPI: CheckFileInfo ─────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET"])
def check_file_info(request, req_id):
    """Метаданные документа. Collabora зовёт это первым делом."""
    req, data = _req_from(request, req_id)
    if req is None:
        return HttpResponseForbidden("Недействительный токен доступа")
    sf = req.document_docx
    return JsonResponse({
        "BaseFileName": req.document_docx_name,
        "Size": sf.size or 0,
        # Версия должна меняться при каждом сохранении, иначе Collabora решит,
        # что файл не трогали, и отдаст свой кэш.
        "Version": (req.generated_at.isoformat() if req.generated_at else str(sf.pk)),
        "LastModifiedTime": (req.generated_at.isoformat() if req.generated_at else ""),
        "OwnerId": str(req.created_by_id or "crm"),
        "UserId": data.get("e") or "crm",
        "UserFriendlyName": data.get("n") or "Сотрудник",
        "UserCanWrite": True,
        # «Сохранить как» и экспорт наружу не нужны — документ живёт в CRM.
        "UserCanNotWriteRelative": True,
        "SupportsUpdate": True,
        "SupportsLocks": False,
        "DisablePrint": False,
        "HideSaveOption": False,
        "PostMessageOrigin": settings.COLLABORA_PUBLIC_URL,
    })


# ── WOPI: GetFile / PutFile ─────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET", "POST"])
def file_contents(request, req_id):
    req, data = _req_from(request, req_id)
    if req is None:
        return HttpResponseForbidden("Недействительный токен доступа")
    if request.method == "GET":
        return _get_file(req)
    return _put_file(request, req, data)


def _get_file(req):
    sf = req.document_docx
    try:
        blob = download_file_from_s3(sf.bucket, sf.key)
    except Exception:
        log.exception("WOPI GetFile: не удалось скачать %s", sf.key)
        raise Http404("Файл документа недоступен в хранилище")
    return HttpResponse(blob, content_type=DOCX_CT)


def _put_file(request, req, data):
    """Collabora вернула изменённый .docx.

    🛑 Читаем поток напрямую (`request.read()`), а не `request.body`: тело
    приходит как application/octet-stream, и у `body` есть потолок
    DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 МБ), в который документ с печатью
    может и не влезть.
    """
    blob = request.read()
    if not blob:
        return HttpResponse(status=400)
    sf = req.document_docx
    try:
        upload_file_to_s3_key(blob, bucket=sf.bucket, key=sf.key, content_type=DOCX_CT)
    except Exception:
        log.exception("WOPI PutFile: не удалось записать %s", sf.key)
        return HttpResponse(status=500)

    from django.utils import timezone
    sf.size = len(blob)
    sf.save(update_fields=["size"])
    req.generated_at = timezone.now()
    req.save(update_fields=["generated_at", "updated_at"])

    # 🛑 PDF здесь НЕ пересобираем. Collabora автосохраняет часто, а конвертация
    # LibreOffice — это секунды CPU на каждое сохранение. Когда собирать PDF,
    # решает юрист кнопкой «Пересобрать PDF» (см. rebuild_pdf_from_docx);
    # до тех пор карточка и таблица показывают «PDF устарел».
    return JsonResponse({"LastModifiedTime": req.generated_at.isoformat()})


# ── Страница-обёртка с редактором (её грузит iframe в карточке) ─────────────

@xframe_options_sameorigin
def editor_frame(request, service_id, req_id):
    """Автоотправляемая форма на Collabora — то, что открывается в iframe.

    Права проверяем ЗДЕСЬ, по обычной сессии: это единственное место, куда
    заходит человек. Дальше документ защищён выданным здесь токеном.
    """
    from apps.core.models import Employee

    from . import services
    from .views import _NotBFL, _bfl_service
    try:
        service = _bfl_service(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    case = services.ensure_case(service)
    req = get_object_or_404(RequestModel, pk=req_id, case=case)
    if not req.document_docx_id:
        return HttpResponseForbidden("Документ ещё не сформирован")

    employee = Employee.objects.filter(user=request.user).first()
    wopi_src = f"{settings.WOPI_INTERNAL_URL.rstrip('/')}/wopi/files/{req.pk}"
    try:
        action = editor_urlsrc()
    except Exception:
        log.exception("Collabora discovery недоступна")
        return render(request, "procedure/wopi_editor.html", {
            "error": "Редактор документов сейчас недоступен — не отвечает сервис "
                     "Collabora. Попробуйте позже или воспользуйтесь правкой по абзацам.",
        })
    sep = "" if action.endswith("?") else ("&" if "?" in action else "?")
    return render(request, "procedure/wopi_editor.html", {
        "action": f"{action}{sep}WOPISrc={quote(wopi_src, safe='')}&lang=ru&closebutton=1",
        "access_token": issue_token(req, employee),
        # WOPI ждёт момент истечения токена в миллисекундах эпохи.
        "ttl_ms": int((time.time() + TOKEN_TTL) * 1000),
        "req": req,
    })
