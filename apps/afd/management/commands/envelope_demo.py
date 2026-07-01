"""Проверочная команда генератора конвертов (не боевой поток).

Запуск:  python manage.py envelope_demo [--size C5|DL|C4|C6]
Печатает в stderr: размер PDF, валидность, mediabox, число страниц, текст.
"""
import io
import sys

from django.core.management.base import BaseCommand

from apps.afd import envelope


class Command(BaseCommand):
    help = "Демо-рендер почтового конверта (проверка генератора)"

    def add_arguments(self, parser):
        parser.add_argument("--size", default="C5", choices=list(envelope.SIZES))

    def handle(self, *args, **opts):
        size = opts["size"]
        sender = envelope.sender_from_executor()
        if not sender.get("name"):
            sender = envelope._party(
                "ООО «ФОЮ Сириус»",
                "400005, г. Волгоград, ул. 7-й Гвардейской, д. 2, офис 203", "400005")
        recipient = envelope._party(
            "ПАО «Сбербанк»", "117312, г. Москва, ул. Вавилова, д. 19", "117312")

        one = envelope.render_envelope(sender, recipient, size=size)
        batch = envelope.render_envelopes(sender, [recipient] * 3, size=size)

        from pypdf import PdfReader
        r1 = PdfReader(io.BytesIO(one))
        rb = PdfReader(io.BytesIO(batch))
        box = r1.pages[0].mediabox

        out = [
            f"size={size} sender={sender}",
            f"single: bytes={len(one)} ispdf={one[:4] == b'%PDF'} pages={len(r1.pages)} "
            f"mediabox={round(float(box.width))}x{round(float(box.height))}pt",
            f"batch(3): bytes={len(batch)} pages={len(rb.pages)}",
        ]
        txt = r1.pages[0].extract_text() or ""
        for needle in ["От кого", "Кому", "ФОЮ", "400005", "Сбербанк", "117312"]:
            out.append(f"  содержит {needle!r}: {needle in txt}")
        sys.stderr.write("\n".join(out) + "\n")
