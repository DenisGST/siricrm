"""UI лотка «Входящие сканы»: список непривязанных сканов, ручная загрузка
и привязка скана к клиенту (папка файл-менеджера + опц. корреспонденция)."""
import mimetypes

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.permissions import can_handle_scans, get_employee
from apps.crm.models import Client, LegalEntity, Service
from apps.files.folder_utils import build_tree, create_default_folders, get_or_create_root
from apps.files.models import ClientFile, ClientFolder, StoredFile
from apps.files.s3_utils import delete_file_from_s3, upload_file_to_s3

from .models import IncomingScan

PAGE_SIZE = 50


def _require_scans(request):
    """Возвращает HttpResponseForbidden, если нет доступа, иначе None."""
    if not can_handle_scans(request.user):
        return HttpResponse("Нет доступа к входящим сканам", status=403)
    return None


def _scan_qs(status, q="", source=""):
    qs = (IncomingScan.objects.filter(status=status)
          .select_related("stored_file", "client", "handled_by__user")
          .order_by("-received_at"))
    q = (q or "").strip()
    if q:
        qs = qs.filter(Q(filename__icontains=q) | Q(source_meta__icontains=q))
    if source in (IncomingScan.SOURCE_AGENT, IncomingScan.SOURCE_MANUAL):
        qs = qs.filter(source=source)
    return qs


def _pending_qs():
    return _scan_qs(IncomingScan.STATUS_PENDING)


def _list_ctx(request):
    """Контекст ленты с фильтрами/пагинацией/архивом для GET-запроса."""
    archive = request.GET.get("archive") == "1"
    q = request.GET.get("q", "")
    source = request.GET.get("source", "")
    status = IncomingScan.STATUS_DISCARDED if archive else IncomingScan.STATUS_PENDING
    qs = _scan_qs(status, q=q, source=source)
    total = qs.count()
    scans = list(qs[:PAGE_SIZE])
    return {
        "scans": scans, "archive": archive, "q": q, "source": source,
        "total": total, "shown": len(scans), "page_size": PAGE_SIZE,
        "truncated": total > PAGE_SIZE,
    }


def _flat_folders(tree, depth=0):
    """Плоский список папок с отступами для <select>."""
    result = []
    for f in tree:
        f.indent = "  " * depth
        result.append(f)
        if getattr(f, "_children", None):
            result += _flat_folders(f._children, depth + 1)
    return result


def _get_scans_folder(client):
    """Папка «Сканы» в корне клиента (создаётся при первом обращении)."""
    if not ClientFolder.objects.filter(client=client).exists():
        create_default_folders(client)
    root = get_or_create_root(client)
    folder, _ = ClientFolder.objects.get_or_create(
        client=client, slug="scans",
        defaults={"parent": root, "name": "Сканы", "order": 5},
    )
    return folder


# ── Панель и список ─────────────────────────────────────────────────────────

@login_required
def inbox(request):
    if (resp := _require_scans(request)) is not None:
        return resp
    return render(request, "scans/inbox.html", _list_ctx(request))


@login_required
def scan_list(request):
    """HTMX-партиал списка (перерисовка/фильтры/пагинация/архив)."""
    if (resp := _require_scans(request)) is not None:
        return resp
    return render(request, "scans/partials/scan_list.html", _list_ctx(request))


@login_required
def pending_count(request):
    """Счётчик непривязанных сканов — для бейджа в меню (HTMX-поллинг)."""
    if not can_handle_scans(request.user):
        return HttpResponse("")
    n = _scan_qs(IncomingScan.STATUS_PENDING).count()
    # Пустой ответ прячет бейдж (CSS :empty), число — показывает.
    return HttpResponse(str(n) if n else "")


# ── Ручная загрузка ─────────────────────────────────────────────────────────

@login_required
@require_POST
def manual_upload(request):
    if (resp := _require_scans(request)) is not None:
        return resp
    for f in request.FILES.getlist("files"):
        content_type = (
            f.content_type or mimetypes.guess_type(f.name)[0]
            or "application/octet-stream"
        )
        file_bytes = f.read()
        try:
            bucket, key = upload_file_to_s3(
                file_bytes, prefix="scans/inbox",
                filename=f.name, content_type=content_type,
            )
        except Exception:
            continue
        stored = StoredFile.objects.create(
            bucket=bucket, key=key, filename=f.name,
            content_type=content_type, size=len(file_bytes),
        )
        IncomingScan.objects.create(
            stored_file=stored, filename=f.name,
            size=len(file_bytes), content_type=content_type,
            source=IncomingScan.SOURCE_MANUAL,
        )
    return render(request, "scans/partials/scan_list.html", _list_ctx(request))


# ── Привязка к клиенту ──────────────────────────────────────────────────────

@login_required
def assign_modal(request, scan_id):
    """Модалка привязки: поиск клиента + выбор папки + опц. корреспонденция."""
    if (resp := _require_scans(request)) is not None:
        return resp
    scan = get_object_or_404(IncomingScan, pk=scan_id, status=IncomingScan.STATUS_PENDING)
    return render(request, "scans/assign_modal.html", {"scan": scan})


@login_required
def client_search(request):
    """HTMX-поиск клиента для модалки привязки."""
    if (resp := _require_scans(request)) is not None:
        return resp
    q = (request.GET.get("q") or "").strip()
    clients = Client.objects.none()
    if len(q) >= 2:
        clients = (
            Client.objects.filter(
                Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(patronymic__icontains=q)
                | Q(phone__icontains=q)
                | Q(phones__phone__icontains=q)
                | Q(username__icontains=q)
            ).distinct().order_by("last_name", "first_name")[:15]
        )
    return render(request, "scans/partials/client_search_results.html", {"clients": clients, "query": q})


@login_required
def client_targets(request):
    """HTMX: после выбора клиента — <select> папок + услуг (для корреспонденции)."""
    if (resp := _require_scans(request)) is not None:
        return resp
    client = get_object_or_404(Client, pk=request.GET.get("client_id"))
    scans_folder = _get_scans_folder(client)
    folders = _flat_folders(build_tree(client))
    services = (
        Service.objects.filter(client=client)
        .select_related("name")
        .order_by("-created_at")
    )
    return render(request, "scans/partials/assign_targets.html", {
        "client": client,
        "folders": folders,
        "default_folder_id": scans_folder.id,
        "services": services,
        "batch": request.GET.get("batch") == "1",
    })


@login_required
@require_POST
def assign(request, scan_id):
    if (resp := _require_scans(request)) is not None:
        return resp
    scan = get_object_or_404(IncomingScan, pk=scan_id, status=IncomingScan.STATUS_PENDING)
    if not scan.stored_file:
        return HttpResponseBadRequest("У скана нет файла в хранилище")

    client = get_object_or_404(Client, pk=request.POST.get("client_id"))
    folder = get_object_or_404(ClientFolder, pk=request.POST.get("folder_id"), client=client)
    emp = get_employee(request.user)

    # Имя файла: даём секретарю переименовать (по умолчанию — исходное).
    name = (request.POST.get("name") or "").strip() or scan.filename

    cf = ClientFile.objects.create(
        folder=folder,
        stored_file=scan.stored_file,
        name=name,
        size=scan.size,
        content_type=scan.content_type,
        uploaded_by=emp,
    )

    # Опционально — завести запись входящей корреспонденции.
    if request.POST.get("make_correspondence") == "on":
        from apps.crm.models import Correspondence
        service = get_object_or_404(Service, pk=request.POST.get("service_id"), client=client)
        counterparty = None
        if request.POST.get("counterparty_id"):
            counterparty = LegalEntity.objects.filter(pk=request.POST["counterparty_id"]).first()
        file_link = request.build_absolute_uri(f"/files/{scan.stored_file_id}/?inline=1")
        Correspondence.objects.create(
            service=service,
            counterparty=counterparty,
            direction="incoming",
            subject_type=(request.POST.get("subject_type") or "").strip(),
            sent_at=request.POST.get("doc_date") or None,
            file_link=file_link,
        )

    scan.status = IncomingScan.STATUS_ASSIGNED
    scan.client = client
    scan.client_file = cf
    scan.handled_by = emp
    scan.handled_at = timezone.now()
    scan.save(update_fields=["status", "client", "client_file", "handled_by", "handled_at"])

    return HttpResponse(status=204, headers={
        "HX-Trigger": "scansChanged",
    })


@login_required
@require_POST
def discard(request, scan_id):
    if (resp := _require_scans(request)) is not None:
        return resp
    scan = get_object_or_404(IncomingScan, pk=scan_id, status=IncomingScan.STATUS_PENDING)
    emp = get_employee(request.user)
    # Файл из лотка отклонён — удаляем из S3 (он никуда не привязан), запись
    # оставляем в архиве для аудита (кто/когда отклонил).
    sf = scan.stored_file
    if sf:
        try:
            delete_file_from_s3(sf.bucket, sf.key)
        except Exception:
            pass
        scan.stored_file = None
        sf.delete()
    scan.status = IncomingScan.STATUS_DISCARDED
    scan.handled_by = emp
    scan.handled_at = timezone.now()
    scan.save(update_fields=["status", "stored_file", "handled_by", "handled_at"])
    return HttpResponse(status=204, headers={"HX-Trigger": "scansChanged"})


@login_required
def counterparty_search(request):
    """HTMX-поиск контрагента (LegalEntity) для формы корреспонденции."""
    if (resp := _require_scans(request)) is not None:
        return resp
    q = (request.GET.get("q") or "").strip()
    items = LegalEntity.objects.none()
    if len(q) >= 2:
        items = LegalEntity.objects.filter(name__icontains=q).order_by("name")[:15]
    return render(request, "scans/partials/counterparty_results.html", {"items": items, "query": q})


# ── Пакетная привязка ───────────────────────────────────────────────────────

def _parse_ids(request, key="ids"):
    """Принимает и повторяющиеся ids=a&ids=b (чекбоксы), и ids=a,b (hidden)."""
    vals = request.POST.getlist(key) or request.GET.getlist(key) or []
    out = []
    for v in vals:
        out += [s for s in v.split(",") if s.strip()]
    return out


@login_required
def batch_modal(request):
    """Модалка пакетной привязки нескольких сканов одному клиенту (папка,
    без корреспонденции)."""
    if (resp := _require_scans(request)) is not None:
        return resp
    ids = _parse_ids(request)
    scans = list(IncomingScan.objects.filter(id__in=ids, status=IncomingScan.STATUS_PENDING))
    if not scans:
        return HttpResponse("<div class='alert alert-warning'>Сканы не выбраны.</div>")
    return render(request, "scans/batch_modal.html", {"scans": scans, "ids": ",".join(str(s.id) for s in scans)})


@login_required
@require_POST
def batch_assign(request):
    if (resp := _require_scans(request)) is not None:
        return resp
    ids = _parse_ids(request)
    client = get_object_or_404(Client, pk=request.POST.get("client_id"))
    folder = get_object_or_404(ClientFolder, pk=request.POST.get("folder_id"), client=client)
    emp = get_employee(request.user)
    scans = IncomingScan.objects.filter(id__in=ids, status=IncomingScan.STATUS_PENDING)
    n = 0
    for scan in scans:
        if not scan.stored_file:
            continue
        cf = ClientFile.objects.create(
            folder=folder, stored_file=scan.stored_file, name=scan.filename,
            size=scan.size, content_type=scan.content_type, uploaded_by=emp,
        )
        scan.status = IncomingScan.STATUS_ASSIGNED
        scan.client = client
        scan.client_file = cf
        scan.handled_by = emp
        scan.handled_at = timezone.now()
        scan.save(update_fields=["status", "client", "client_file", "handled_by", "handled_at"])
        n += 1
    return HttpResponse(status=204, headers={"HX-Trigger": "scansChanged"})
