"""UI лотка «Входящие сканы»: список непривязанных сканов, ручная загрузка
и привязка скана к клиенту (папка файл-менеджера + опц. корреспонденция)."""
import mimetypes

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.permissions import can_handle_scans, get_employee
from apps.crm.models import Client, LegalEntity, Service
from apps.files.folder_utils import build_tree, create_default_folders, get_or_create_root
from apps.files.models import ClientFile, ClientFolder, StoredFile
from apps.files.s3_utils import upload_file_to_s3

from .models import IncomingScan


def _require_scans(request):
    """Возвращает HttpResponseForbidden, если нет доступа, иначе None."""
    if not can_handle_scans(request.user):
        return HttpResponse("Нет доступа к входящим сканам", status=403)
    return None


def _pending_qs():
    return (
        IncomingScan.objects.filter(status=IncomingScan.STATUS_PENDING)
        .select_related("stored_file")
        .order_by("-received_at")
    )


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
    scans = list(_pending_qs())
    return render(request, "scans/inbox.html", {"scans": scans})


@login_required
def scan_list(request):
    """HTMX-партиал списка (перерисовка после привязки/загрузки)."""
    if (resp := _require_scans(request)) is not None:
        return resp
    scans = list(_pending_qs())
    return render(request, "scans/partials/scan_list.html", {"scans": scans})


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
    scans = list(_pending_qs())
    return render(request, "scans/partials/scan_list.html", {"scans": scans})


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
    counterparties = LegalEntity.objects.order_by("name")[:500]
    return render(request, "scans/partials/assign_targets.html", {
        "client": client,
        "folders": folders,
        "default_folder_id": scans_folder.id,
        "services": services,
        "counterparties": counterparties,
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
    scan.status = IncomingScan.STATUS_DISCARDED
    scan.handled_by = emp
    scan.handled_at = timezone.now()
    scan.save(update_fields=["status", "handled_by", "handled_at"])
    scans = list(_pending_qs())
    return render(request, "scans/partials/scan_list.html", {"scans": scans})
