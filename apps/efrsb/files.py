"""Скачивание приложенных к публикации ЕФРСБ файлов (/files/archive → S3).

Архив ZIP распаковываем, каждый файл → upload_file_to_s3 + StoredFile +
EfrsbPublicationFile. Идемпотентно по (publication, name).
"""
from __future__ import annotations

import io
import logging
import zipfile

from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3

from . import client
from .models import EfrsbPublication, EfrsbPublicationFile

log = logging.getLogger(__name__)


def download_publication_files(pub: EfrsbPublication, *, only_safe: bool = True) -> int:
    """Скачать ZIP-архив файлов публикации и разложить в S3. Возвращает кол-во новых."""
    if not pub.fedresurs_guid or pub.is_locked:
        return 0
    if pub.kind == EfrsbPublication.KIND_REPORT:
        archive = client.get_report_files(pub.fedresurs_guid, only_safe=only_safe)
    else:
        archive = client.get_message_files(pub.fedresurs_guid, only_safe=only_safe)
    if not archive:
        return 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile:
        log.warning("efrsb files: не ZIP для guid=%s", pub.fedresurs_guid)
        return 0

    created = 0
    existing = set(pub.files.values_list("name", flat=True))
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.split("/")[-1] or info.filename
        if name in existing:
            continue
        data = zf.read(info)
        if not data:
            continue
        bucket, key = upload_file_to_s3(data, prefix="efrsb/files", filename=name)
        sf = StoredFile.objects.create(
            bucket=bucket, key=key, filename=name, size=len(data),
        )
        EfrsbPublicationFile.objects.create(
            publication=pub, stored_file=sf, name=name, is_safe=only_safe,
        )
        existing.add(name)
        created += 1
    return created
