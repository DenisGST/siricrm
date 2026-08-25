"""HTTP-приём данных с АТС Asterisk.

Агент ``tools/pbx-agent/`` живёт на самой АТС: читает CDR, конвертирует
wav→mp3 и пушит сюда. Поллинга со стороны CRM нет — АТС за NAT, ходить к ней
некуда.

🛑 Ключи S3 на АТС НЕ кладём: файл приходит сюда, а в хранилище его отправляет
уже CRM. У АТС наружу открыт root-вход по паролю (решение владельца), поэтому
доступ к боевому медиа-бакету оттуда был бы неприемлемым риском.

Аутентификация — Bearer-токен ``PBX_AGENT_TOKEN`` (env сервера), как у
scan-агента. Токен узкий: даёт только приём звонков и записей.
"""
import json
import logging
import os

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3

from . import missed as missed_mod
from .models import Call, MissedCall
from .services import parse_pbx_datetime, upsert_call

logger = logging.getLogger(__name__)

# 30-минутный разговор в mp3 32 кбит/с — около 7 МБ. Потолок с запасом.
MAX_RECORDING_BYTES = 64 * 1024 * 1024
# Ограничение пачки метаданных за один запрос — чтобы бэкфилл 13k звонков
# не пришёл одним гигантским телом.
MAX_CALLS_PER_BATCH = 500

S3_PREFIX = "telephony/records"
# Голосовые лежат отдельно от разговоров: у них другой смысл и другой срок
# хранения — это обращение клиента, а не запись беседы.
S3_VOICEMAIL_PREFIX = "telephony/voicemail"


def _check_token(request) -> bool:
    expected = os.environ.get("PBX_AGENT_TOKEN", "")
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
    return JsonResponse({"status": "ok", "service": "siricrm-pbx-intake"})


@csrf_exempt
@require_http_methods(["POST"])
def agent_calls(request):
    """Пачка метаданных звонков из CDR.

    Тело — JSON ``{"calls": [{uniqueid, calldate, src, dst, ...}, ...]}``.
    Идемпотентно: ключ ``uniqueid``, повтор обновляет запись.
    Ответ — ``{"created": N, "updated": M, "failed": [...]}``.
    """
    if not _check_token(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "bad_json"}, status=400)

    rows = payload.get("calls")
    if not isinstance(rows, list):
        return JsonResponse({"error": "calls_required"}, status=400)
    if len(rows) > MAX_CALLS_PER_BATCH:
        return JsonResponse({"error": "batch_too_large", "max": MAX_CALLS_PER_BATCH}, status=413)

    created = updated = 0
    failed = []
    for row in rows:
        if not isinstance(row, dict):
            failed.append({"row": str(row)[:80], "error": "not_an_object"})
            continue
        try:
            with transaction.atomic():
                _, was_created = upsert_call(row)
        except Exception as exc:
            # Один кривой звонок не должен ронять всю пачку.
            logger.warning("pbx intake: звонок %s не принят: %s", row.get("uniqueid"), exc)
            failed.append({"uniqueid": row.get("uniqueid"), "error": str(exc)[:200]})
            continue
        if was_created:
            created += 1
        else:
            updated += 1

    return JsonResponse({"created": created, "updated": updated, "failed": failed})


@csrf_exempt
@require_http_methods(["POST"])
def agent_recording(request):
    """Запись разговора (mp3) для уже принятого звонка.

    multipart/form-data: ``uniqueid`` + ``file``.
    Повторная отправка заменяет ссылку на файл; старый объект в S3 остаётся
    (как и во всех остальных разделах — физически из бакета ничего не трём).
    """
    if not _check_token(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    uniqueid = (request.POST.get("uniqueid") or "").strip()
    if not uniqueid:
        return JsonResponse({"error": "uniqueid_required"}, status=400)

    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"error": "file_required"}, status=400)
    if upload.size and upload.size > MAX_RECORDING_BYTES:
        return JsonResponse({"error": "file_too_large"}, status=413)

    call = Call.objects.filter(uniqueid=uniqueid).first()
    if call is None:
        # Агент всегда шлёт метаданные раньше файла; если звонка нет — это
        # рассинхрон, и молча создавать пустышку нельзя: у неё не будет ни
        # клиента, ни сотрудника, ни времени.
        return JsonResponse({"error": "call_not_found", "uniqueid": uniqueid}, status=404)

    file_bytes = upload.read()
    if not file_bytes:
        return JsonResponse({"error": "empty_file"}, status=400)

    filename = f"{call.started_at:%Y-%m-%d_%H-%M-%S}_{call.src or 'x'}-{call.dst or 'x'}.mp3"
    filename = filename.replace("/", "_")
    try:
        bucket, key = upload_file_to_s3(
            file_bytes, prefix=S3_PREFIX,
            filename=filename, content_type="audio/mpeg",
        )
    except Exception as exc:
        logger.error("pbx intake: не удалось положить запись %s в S3: %s", uniqueid, exc)
        return JsonResponse({"error": "storage_failed"}, status=502)

    stored = StoredFile.objects.create(
        bucket=bucket, key=key, filename=filename,
        content_type="audio/mpeg", size=len(file_bytes),
    )
    call.recording = stored
    call.has_recording_on_pbx = True
    call.save(update_fields=["recording", "has_recording_on_pbx", "updated_at"])

    return JsonResponse(
        {"id": str(call.pk), "stored_file": str(stored.pk), "size": len(file_bytes)},
        status=201,
    )


@csrf_exempt
@require_http_methods(["POST"])
def agent_missed(request):
    """Пропущенный звонок или голосовое сообщение — по горячим следам.

    Шлёт не таймерный агент, а сам диалплан АТС (``sc_notify_crm.sh`` из
    ``System()``), поэтому уведомление уходит сотрудникам за секунды, а не
    к следующему проходу выгрузки CDR.

    Тело — JSON::

        {"event": "missed"|"voicemail", "group": "cc", "linkedid": "...",
         "uniqueid": "...", "phone": "+79...", "extension": "",
         "occurred_at": "2026-08-25 14:07:52",
         "voicemail_file": "...wav", "voicemail_seconds": 17}

    Идемпотентно по ``linkedid``: повтор (АТС не дождалась ответа и послала
    второй раз) обновит запись и не разошлёт уведомление заново.
    """
    if not _check_token(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "bad_json"}, status=400)

    linkedid = (payload.get("linkedid") or "").strip()
    if not linkedid:
        return JsonResponse({"error": "linkedid_required"}, status=400)

    event = (payload.get("event") or "missed").strip().lower()
    kind = (MissedCall.KIND_VOICEMAIL if event == "voicemail"
            else MissedCall.KIND_MISSED)
    try:
        seconds = int(payload.get("voicemail_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0

    try:
        record, created = missed_mod.register(
            linkedid=linkedid,
            uniqueid=(payload.get("uniqueid") or "").strip(),
            occurred_at=parse_pbx_datetime(payload.get("occurred_at") or ""),
            phone=(payload.get("phone") or "").strip(),
            kind=kind,
            group_code=(payload.get("group") or "").strip(),
            extension=(payload.get("extension") or "").strip(),
            dcontext=(payload.get("dcontext") or "").strip(),
            voicemail_file=(payload.get("voicemail_file") or "").strip(),
            voicemail_seconds=seconds,
        )
    except Exception as exc:
        logger.exception("pbx intake: пропущенный %s не принят", linkedid)
        return JsonResponse({"error": str(exc)[:200]}, status=400)

    return JsonResponse(
        {"id": str(record.pk), "created": created, "kind": record.kind,
         "client": str(record.client) if record.client_id else None},
        status=201 if created else 200,
    )


@csrf_exempt
@require_http_methods(["POST"])
def agent_voicemail(request):
    """Голосовое сообщение (mp3) к уже принятому пропущенному.

    multipart/form-data: ``linkedid`` или ``voicemail_file`` (имя wav на АТС,
    по которому запись искалась) + ``file``.

    🛑 Файл шлёт таймерный агент, а не диалплан: MixMonitor дописывает wav до
    самого конца сообщения, и отправлять его в момент события рано — на АТС
    в этот момент лежит незакрытый файл.
    """
    if not _check_token(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    linkedid = (request.POST.get("linkedid") or "").strip()
    vm_name = (request.POST.get("voicemail_file") or "").strip()
    if not linkedid and not vm_name:
        return JsonResponse({"error": "linkedid_or_file_required"}, status=400)

    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"error": "file_required"}, status=400)
    if upload.size and upload.size > MAX_RECORDING_BYTES:
        return JsonResponse({"error": "file_too_large"}, status=413)

    record = (MissedCall.objects.filter(linkedid=linkedid).first() if linkedid
              else MissedCall.objects.filter(voicemail_file=vm_name).first())
    if record is None:
        # Как и с записями разговоров: пустышку не создаём — у неё не было бы
        # ни времени, ни номера, ни направления.
        return JsonResponse({"error": "missed_call_not_found"}, status=404)

    file_bytes = upload.read()
    if not file_bytes:
        return JsonResponse({"error": "empty_file"}, status=400)

    filename = f"voicemail_{record.occurred_at:%Y-%m-%d_%H-%M-%S}_{record.phone or 'x'}.mp3"
    filename = filename.replace("/", "_")
    try:
        bucket, key = upload_file_to_s3(
            file_bytes, prefix=S3_VOICEMAIL_PREFIX,
            filename=filename, content_type="audio/mpeg",
        )
    except Exception as exc:
        logger.error("pbx intake: голосовое %s не легло в S3: %s", record.pk, exc)
        return JsonResponse({"error": "storage_failed"}, status=502)

    stored = StoredFile.objects.create(
        bucket=bucket, key=key, filename=filename,
        content_type="audio/mpeg", size=len(file_bytes),
    )
    record.recording = stored
    if record.kind != MissedCall.KIND_VOICEMAIL:
        record.kind = MissedCall.KIND_VOICEMAIL
    record.save(update_fields=["recording", "kind", "updated_at"])

    return JsonResponse(
        {"id": str(record.pk), "stored_file": str(stored.pk), "size": len(file_bytes)},
        status=201,
    )
