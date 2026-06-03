"""Диагностика: что kad отдаёт на PDF endpoint + проверка download_pdf."""
import requests
from django.core.management.base import BaseCommand

from apps.arbitr.parsers.kad import KadSession, KAD_BASE_URL


class Command(BaseCommand):
    help = "Скачивает PDF через KadSession.download_pdf (Chrome-flow)."

    def add_arguments(self, parser):
        parser.add_argument("url")
        parser.add_argument("--referer", default="",
                            help="URL карточки дела — без него kad даёт ПравоКапчу")
        parser.add_argument("--raw", action="store_true",
                            help="Показать что отдаёт raw GET через requests")

    def handle(self, url, referer, raw, **opts):
        with KadSession() as s:
            s._warm_up()
            if raw:
                cookies = {c["name"]: c["value"] for c in s.driver.get_cookies()}
                ua = s.driver.execute_script("return navigator.userAgent")
                r = requests.get(
                    url, cookies=cookies,
                    headers={
                        "User-Agent": ua,
                        "Referer": KAD_BASE_URL + "/",
                        "Accept": "application/pdf,application/octet-stream,*/*",
                    },
                    timeout=30, allow_redirects=True,
                )
                self.stdout.write(f"RAW STATUS: {r.status_code}")
                self.stdout.write(f"RAW CT: {r.headers.get('Content-Type')!r}")
                self.stdout.write(f"RAW LEN: {len(r.content)}")
                self.stdout.write(r.content[:1500].decode("utf-8", errors="ignore"))
                return

            content, ct = s.download_pdf(url, timeout=90, referer=referer)
            self.stdout.write(self.style.SUCCESS(
                f"OK: ct={ct!r} bytes={len(content)} "
                f"magic={content[:5]!r}"
            ))
