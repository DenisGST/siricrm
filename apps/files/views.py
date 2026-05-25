import mimetypes

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.crm.models import Client
from apps.core.models import Employee
from .models import ClientFile, ClientFolder, StoredFile
from .folder_utils import build_tree, create_default_folders, get_folder_path
from .s3_utils import (
    delete_file_from_s3, get_presigned_url, upload_file_to_s3,
)


def _current_employee(request):
    try:
        return Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        return None


# ── Модалка файлового менеджера ───────────────────────────────────────────────

@login_required
def file_manager(request, client_pk):
    """Файловый менеджер клиента. Поддерживает ?file=<uuid> — открыть
    папку конкретного файла и подсветить запись."""
    from django.shortcuts import render
    from .models import ClientFile

    client = get_object_or_404(Client, pk=client_pk)
    # Создаём папки по умолчанию если их нет
    if not ClientFolder.objects.filter(client=client).exists():
        create_default_folders(client)
    tree = build_tree(client)
    root = next((f for f in tree), None)

    active = root
    highlight_id = ""
    target_file_id = request.GET.get("file") or ""
    if target_file_id:
        cf = (
            ClientFile.objects.filter(pk=target_file_id, folder__client=client)
            .select_related("folder").first()
        )
        if cf is not None:
            active = cf.folder
            highlight_id = str(cf.pk)

    files = list(active.files.select_related("uploaded_by__user").all()) if active else []
    breadcrumb = get_folder_path(active) if active else []
    all_folders = _flat_folders(tree)
    return render(request, "files/manager.html", {
        "client": client,
        "tree": tree,
        "active": active,
        "files": files,
        "breadcrumb": breadcrumb,
        "all_folders": all_folders,
        "highlight_id": highlight_id,
    })


@login_required
def file_search(request, client_pk):
    """HTMX-партиал: список файлов клиента, отфильтрованный по имени."""
    from django.shortcuts import render
    from .models import ClientFile

    client = get_object_or_404(Client, pk=client_pk)
    q = (request.GET.get("q") or "").strip()
    qs = (
        ClientFile.objects.filter(folder__client=client)
        .select_related("folder", "uploaded_by__user")
    )
    for word in q.split():
        qs = qs.filter(name__icontains=word)
    files = list(qs.order_by("folder__name", "name")[:200])
    return render(request, "files/partials/search_results.html", {
        "client": client, "files": files, "q": q,
    })


@login_required
def folder_contents(request, folder_pk):
    from django.shortcuts import render
    folder = get_object_or_404(ClientFolder, pk=folder_pk)
    client = folder.client
    tree   = build_tree(client)
    files  = list(folder.files.select_related("uploaded_by__user").all())
    breadcrumb = get_folder_path(folder)
    return render(request, "files/partials/contents.html", {
        "client": client,
        "tree": tree,
        "active": folder,
        "files": files,
        "breadcrumb": breadcrumb,
    })


# ── Загрузка файла ────────────────────────────────────────────────────────────

@login_required
@require_POST
def file_upload(request, folder_pk):
    from django.shortcuts import render
    folder = get_object_or_404(ClientFolder, pk=folder_pk)
    emp    = _current_employee(request)
    uploaded = request.FILES.getlist("files")
    for f in uploaded:
        content_type = f.content_type or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        file_bytes = f.read()
        try:
            bucket, key = upload_file_to_s3(
                file_bytes,
                prefix=f"clients/{folder.client_id}/files",
                filename=f.name,
                content_type=content_type,
            )
        except Exception:
            continue
        stored = StoredFile.objects.create(
            bucket=bucket, key=key,
            filename=f.name,
            content_type=content_type,
            size=len(file_bytes),
        )
        ClientFile.objects.create(
            folder=folder,
            stored_file=stored,
            name=f.name,
            size=len(file_bytes),
            content_type=content_type,
            uploaded_by=emp,
        )
    # Вернуть обновлённое содержимое папки
    files = list(folder.files.select_related("uploaded_by__user").all())
    breadcrumb = get_folder_path(folder)
    tree = build_tree(folder.client)
    return render(request, "files/partials/contents.html", {
        "client": folder.client,
        "tree": tree,
        "active": folder,
        "files": files,
        "breadcrumb": breadcrumb,
    })


# ── Скачать файл ──────────────────────────────────────────────────────────────

@login_required
def file_download(request, file_pk):
    cf = get_object_or_404(ClientFile, pk=file_pk)
    if not cf.stored_file:
        return HttpResponse("Файл не найден в хранилище", status=404)
    url = get_presigned_url(cf.stored_file.bucket, cf.stored_file.key, expiration=600)
    return redirect(url)


# ── Удалить файл ──────────────────────────────────────────────────────────────

@login_required
@require_POST
def file_delete(request, file_pk):
    from django.shortcuts import render
    cf     = get_object_or_404(ClientFile, pk=file_pk)
    folder = cf.folder
    if cf.stored_file:
        delete_file_from_s3(cf.stored_file.bucket, cf.stored_file.key)
        cf.stored_file.delete()
    cf.delete()
    files = list(folder.files.select_related("uploaded_by__user").all())
    breadcrumb = get_folder_path(folder)
    tree = build_tree(folder.client)
    return render(request, "files/partials/contents.html", {
        "client": folder.client,
        "tree": tree,
        "active": folder,
        "files": files,
        "breadcrumb": breadcrumb,
    })


# ── Переместить файл ──────────────────────────────────────────────────────────

@login_required
@require_POST
def file_move(request, file_pk):
    from django.shortcuts import render
    cf        = get_object_or_404(ClientFile, pk=file_pk)
    target_pk = request.POST.get("target_folder")
    if not target_pk:
        return HttpResponse("target_folder required", status=400)
    target = get_object_or_404(ClientFolder, pk=target_pk, client=cf.folder.client)
    old_folder = cf.folder
    cf.folder = target
    cf.save(update_fields=["folder"])
    files = list(old_folder.files.select_related("uploaded_by__user").all())
    breadcrumb = get_folder_path(old_folder)
    tree = build_tree(old_folder.client)
    return render(request, "files/partials/contents.html", {
        "client": old_folder.client,
        "tree": tree,
        "active": old_folder,
        "files": files,
        "breadcrumb": breadcrumb,
    })


# ── Создать папку ─────────────────────────────────────────────────────────────

@login_required
@require_POST
def folder_create(request, parent_pk):
    from django.shortcuts import render
    parent = get_object_or_404(ClientFolder, pk=parent_pk)
    name   = (request.POST.get("name") or "").strip()
    if not name:
        return HttpResponse("name required", status=400)
    ClientFolder.objects.create(client=parent.client, parent=parent, name=name)
    tree = build_tree(parent.client)
    files = list(parent.files.select_related("uploaded_by__user").all())
    breadcrumb = get_folder_path(parent)
    return render(request, "files/partials/contents.html", {
        "client": parent.client,
        "tree": tree,
        "active": parent,
        "files": files,
        "breadcrumb": breadcrumb,
    })


# ── Переименовать папку ───────────────────────────────────────────────────────

@login_required
@require_POST
def folder_rename(request, folder_pk):
    folder = get_object_or_404(ClientFolder, pk=folder_pk)
    name   = (request.POST.get("name") or "").strip()
    if not name:
        return HttpResponse("name required", status=400)
    folder.name = name
    folder.save(update_fields=["name"])
    return JsonResponse({"ok": True, "name": folder.name})


# ── Удалить папку ─────────────────────────────────────────────────────────────

@login_required
@require_POST
def folder_delete(request, folder_pk):
    from django.shortcuts import render
    folder = get_object_or_404(ClientFolder, pk=folder_pk)
    # Запрещаем удалять системные папки
    if folder.slug:
        return HttpResponse("Системную папку нельзя удалить", status=403)
    client = folder.client
    parent = folder.parent
    # Удаляем все файлы в папке (рекурсивно через cascade, но S3 чистим вручную)
    for cf in ClientFile.objects.filter(folder__in=_all_folder_ids(folder)):
        if cf.stored_file:
            delete_file_from_s3(cf.stored_file.bucket, cf.stored_file.key)
            cf.stored_file.delete()
    folder.delete()
    active = parent or ClientFolder.objects.filter(client=client, parent=None).first()
    tree = build_tree(client)
    files = list(active.files.select_related("uploaded_by__user").all()) if active else []
    breadcrumb = get_folder_path(active) if active else []
    return render(request, "files/partials/contents.html", {
        "client": client,
        "tree": tree,
        "active": active,
        "files": files,
        "breadcrumb": breadcrumb,
    })


# ── Предпросмотр файла ────────────────────────────────────────────────────────

_PREVIEWABLE = {
    "image":  {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"},
    "pdf":    {"pdf"},
    "video":  {"mp4", "webm", "mov", "avi"},
    "audio":  {"mp3", "wav", "ogg", "m4a", "flac"},
    "text":   {"txt", "csv", "log", "json", "xml", "html", "md"},
}

def _preview_type(filename, content_type=""):
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    for kind, exts in _PREVIEWABLE.items():
        if ext in exts:
            return kind
    ct = (content_type or "").lower()
    if ct.startswith("image/"):    return "image"
    if ct == "application/pdf":    return "pdf"
    if ct.startswith("video/"):    return "video"
    if ct.startswith("audio/"):    return "audio"
    if ct.startswith("text/"):     return "text"
    return None


@login_required
def file_preview(request, file_pk):
    from django.shortcuts import render
    cf = get_object_or_404(ClientFile, pk=file_pk)
    if not cf.stored_file:
        return HttpResponse("Файл не найден в хранилище", status=404)
    url  = get_presigned_url(cf.stored_file.bucket, cf.stored_file.key, expiration=1800)
    kind = _preview_type(cf.name, cf.content_type)
    text_content = None
    if kind == "text":
        try:
            from apps.files.s3_utils import download_file_from_s3
            raw = download_file_from_s3(cf.stored_file.bucket, cf.stored_file.key)
            text_content = raw.decode("utf-8", errors="replace")[:50_000]
        except Exception:
            kind = None
    return render(request, "files/partials/preview.html", {
        "file": cf, "url": url, "kind": kind, "text_content": text_content,
    })


def _flat_folders(tree, depth=0):
    """Плоский список папок с отступами для select."""
    result = []
    for f in tree:
        f.indent = "  " * depth
        result.append(f)
        if hasattr(f, "_children") and f._children:
            result += _flat_folders(f._children, depth + 1)
    return result


def _all_folder_ids(folder):
    """Рекурсивно собирает все дочерние папки + саму папку."""
    ids = [folder]
    for child in ClientFolder.objects.filter(parent=folder):
        ids += _all_folder_ids(child)
    return ids


# ── Старый download_file для StoredFile (чат) ─────────────────────────────────

@login_required
def download_stored_file(request, file_id):
    stored = get_object_or_404(StoredFile, pk=file_id)
    url = get_presigned_url(stored.bucket, stored.key, expiration=300)
    return redirect(url)
