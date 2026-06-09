"""HTTP-приём сканов от локального агента на офисном ПК.

Агент следит за сетевой папкой МФУ и шлёт каждый новый PDF сюда
multipart-запросом с Bearer-токеном (``SCAN_AGENT_TOKEN`` в env сервера).
Поллинг/IMAP не нужны — агент пушит файлы сам.
"""
import mimetypes
import os

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3

from .models import IncomingScan

# 50 МБ — потолок одного скана (многостраничные PDF), защита от мусора.
MAX_SCAN_BYTES = 50 * 1024 * 1024


def _check_token(request) -> bool:
    expected = os.environ.get("SCAN_AGENT_TOKEN", "")
    if not expected:
        return False
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Bearer "):
        return False
    return auth.removeprefix("Bearer ") == expected


@csrf_exempt
@require_http_methods(["GET"])
def agent_ping(request):
    if not _check_token(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse({"status": "ok", "service": "siricrm-scan-intake"})


@csrf_exempt
@require_http_methods(["POST"])
def agent_intake(request):
    """Приём одного скана. Тело — multipart/form-data:
        file   — сам файл (обязательно)
        device — метка устройства/папки (опционально)
    Ответ: {id, filename}.
    """
    if not _check_token(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"error": "file_required"}, status=400)
    if upload.size and upload.size > MAX_SCAN_BYTES:
        return JsonResponse({"error": "file_too_large"}, status=413)

    filename = upload.name or "scan.pdf"
    content_type = (
        upload.content_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    file_bytes = upload.read()

    try:
        bucket, key = upload_file_to_s3(
            file_bytes, prefix="scans/inbox",
            filename=filename, content_type=content_type,
        )
    except Exception:
        return JsonResponse({"error": "storage_failed"}, status=502)

    stored = StoredFile.objects.create(
        bucket=bucket, key=key, filename=filename,
        content_type=content_type, size=len(file_bytes),
    )
    scan = IncomingScan.objects.create(
        stored_file=stored, filename=filename,
        size=len(file_bytes), content_type=content_type,
        source=IncomingScan.SOURCE_AGENT,
        source_meta=(request.POST.get("device") or "")[:255],
    )
    _notify_new_scan(filename)
    return JsonResponse({"id": str(scan.pk), "filename": filename}, status=201)


def _notify_new_scan(filename: str):
    """Тост сотрудникам с доступом к лотку — пришёл новый скан."""
    try:
        from apps.core.models import Employee
        from apps.realtime.utils import push_toast
        handlers = (Employee.objects.filter(can_handle_scans=True, user__is_active=True)
                    .select_related("user"))
        for emp in handlers:
            if emp.user_id:
                push_toast(emp.user, f"🖨️ Новый скан: {filename}", level="info")
    except Exception:
        pass
