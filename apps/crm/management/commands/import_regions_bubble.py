# apps/crm/management/commands/import_regions_bubble.py


import requests
from django.core.management.base import BaseCommand
from apps.crm.models import Region


BUBBLE_API_TOKEN = "BUBBLE_API_TOKEN"
BUBBLE_API_URL = "https://siricrmdev.ru/version-test/api/1.1/obj/Region"



class Command(BaseCommand):
    help = 'Импорт регионов из Bubble.io'

    def handle(self, *args, **options):
        headers = {"Authorization": f"Bearer {BUBBLE_API_TOKEN}"}
        cursor = 0
        limit = 100
        total_imported = 0
        total_skipped = 0

        self.stdout.write("Начинаем импорт регионов из Bubble.io...")

        while True:
            params = {"limit": limit, "cursor": cursor}
            response = requests.get(BUBBLE_API_URL, headers=headers, params=params)

            if response.status_code != 200:
                self.stderr.write(f"Ошибка API: {response.status_code} — {response.text}")
                break

            data = response.json()["response"]
            results = data["results"]

            if not results:
                break

            for item in results:
                number = item.get("numberRegion")
                name = item.get("nameRegion", "").strip()
                court_name = item.get("nameSud", "").strip()

                if not number or not name:
                    self.stdout.write(f"  Пропущен: {item.get('_id')} — нет номера или названия")
                    total_skipped += 1
                    continue

                obj, created = Region.objects.update_or_create(
                    number=number,
                    defaults={
                        "name": name,
                        "court_name": court_name,
                        # адрес и реквизиты в Bubble нет — оставим пустыми
                        "court_address": "",
                        "court_payment_details": "",
                    }
                )

                status = "создан" if created else "обновлён"
                self.stdout.write(f"  Регион {number} ({name}) — {status}")
                total_imported += 1

            # Пагинация
            remaining = data.get("remaining", 0)
            if remaining == 0:
                break
            cursor += limit

        self.stdout.write(
            self.style.SUCCESS(
                f"\nГотово! Импортировано: {total_imported}, пропущено: {total_skipped}"
            )
        )