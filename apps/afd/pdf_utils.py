"""Конвертация .docx → .pdf (LibreOffice headless) и склейка PDF (pypdf)."""
import io
import logging
import os
import subprocess
import tempfile
import uuid

log = logging.getLogger(__name__)

# Бинарь LibreOffice (устанавливается пакетом libreoffice-writer в Dockerfile).
_SOFFICE = os.environ.get("SOFFICE_BIN", "soffice")


class PdfConvertError(RuntimeError):
    pass


def docx_to_pdf(docx_bytes: bytes, *, timeout: int = 120) -> bytes:
    """Конвертирует .docx в .pdf через `soffice --headless --convert-to pdf`.

    Каждый вызов использует отдельный UserInstallation-профиль, чтобы
    параллельные конвертации не блокировали друг друга.
    """
    with tempfile.TemporaryDirectory(prefix="afd_pdf_") as tmp:
        src = os.path.join(tmp, "doc.docx")
        with open(src, "wb") as f:
            f.write(docx_bytes)
        profile = os.path.join(tmp, "profile")
        cmd = [
            _SOFFICE, "--headless", "--norestore", "--nolockcheck",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf", "--outdir", tmp, src,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise PdfConvertError(f"LibreOffice timeout: {e}") from e
        out_pdf = os.path.join(tmp, "doc.pdf")
        if proc.returncode != 0 or not os.path.exists(out_pdf):
            raise PdfConvertError(
                "LibreOffice не смог сконвертировать в PDF: "
                f"rc={proc.returncode} stderr={proc.stderr.decode('utf-8', 'replace')[:500]}"
            )
        with open(out_pdf, "rb") as f:
            return f.read()


def merge_pdfs(pdf_chunks: list[bytes]) -> bytes:
    """Склеивает несколько PDF (bytes) в один. Пустые/битые куски пропускает."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for chunk in pdf_chunks:
        if not chunk:
            continue
        try:
            reader = PdfReader(io.BytesIO(chunk))
            for page in reader.pages:
                writer.add_page(page)
        except Exception:
            log.exception("merge_pdfs: пропускаю битый PDF-кусок")
            continue
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def new_token() -> str:
    return uuid.uuid4().hex
