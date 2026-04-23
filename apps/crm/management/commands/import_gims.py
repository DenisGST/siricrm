"""
Импорт ФКУ «Центр ГИМС МЧС России» по каждому субъекту РФ + центральный ФКУ ЦОД.

У каждого регионального Центра ГИМС — свой ИНН (это самостоятельные ФКУ).
Список берём перебором DaData suggest/party с разными запросами:
  - "ФКУ Центр ГИМС МЧС"
  - "Центр ГИМС МЧС России по <регион>"
Дедуп по ИНН. Руководителя не заполняем.
"""
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind
from apps.crm.management.commands.import_cbr_banks import (
    enrich_from_dadata,
    map_entity_type,
)
from apps.crm.management.commands.import_myfin_mfo import dadata_find_by_inn


SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"

# Список субъектов РФ (для подстановки в имя региона).
REGIONS = [
    "Республике Адыгея", "Республике Башкортостан", "Республике Бурятия",
    "Республике Алтай", "Республике Дагестан", "Республике Ингушетия",
    "Кабардино-Балкарской Республике", "Республике Калмыкия",
    "Карачаево-Черкесской Республике", "Республике Карелия", "Республике Коми",
    "Республике Марий Эл", "Республике Мордовия", "Республике Саха",
    "Республике Северная Осетия", "Республике Татарстан", "Республике Тыва",
    "Удмуртской Республике", "Республике Хакасия", "Чеченской Республике",
    "Чувашской Республике", "Алтайскому краю", "Краснодарскому краю",
    "Красноярскому краю", "Приморскому краю", "Ставропольскому краю",
    "Хабаровскому краю", "Амурской области", "Архангельской области",
    "Астраханской области", "Белгородской области", "Брянской области",
    "Владимирской области", "Волгоградской области", "Вологодской области",
    "Воронежской области", "Ивановской области", "Иркутской области",
    "Калининградской области", "Калужской области", "Камчатскому краю",
    "Кемеровской области", "Кировской области", "Костромской области",
    "Курганской области", "Курской области", "Ленинградской области",
    "Липецкой области", "Магаданской области", "Московской области",
    "Мурманской области", "Нижегородской области", "Новгородской области",
    "Новосибирской области", "Омской области", "Оренбургской области",
    "Орловской области", "Пензенской области", "Пермскому краю",
    "Псковской области", "Ростовской области", "Рязанской области",
    "Самарской области", "Саратовской области", "Сахалинской области",
    "Свердловской области", "Смоленской области", "Тамбовской области",
    "Тверской области", "Томской области", "Тульской области",
    "Тюменской области", "Ульяновской области", "Челябинской области",
    "Забайкальскому краю", "Ярославской области", "г. Москве",
    "г. Санкт-Петербургу", "Еврейской автономной области",
    "Ненецкому автономному округу", "Ханты-Мансийскому автономному округу",
    "Чукотскому автономному округу", "Ямало-Ненецкому автономному округу",
    "Республике Крым", "г. Севастополю",
]


def dadata_suggest(query: str, token: str, count: int = 20) -> list[dict]:
    try:
        r = requests.post(
            SUGGEST_URL,
            json={"query": query, "count": count, "type": "LEGAL"},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {token}",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("suggestions") or []
    except Exception:
        return []


def is_gims(s: dict) -> bool:
    """Фильтруем — в имени должно быть 'ГИМС' и 'МЧС'."""
    v = (s.get("value") or "").upper()
    return "ГИМС" in v and "МЧС" in v


class Command(BaseCommand):
    help = "Импорт ФКУ Центр ГИМС МЧС России по субъектам РФ"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--sleep", type=float, default=0.15)

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        sleep_s = opts["sleep"]
        token = getattr(settings, "DADATA_API_KEY", "") or ""
        if not token:
            self.stdout.write(self.style.ERROR("Нет DADATA_API_KEY"))
            return

        kind = LegalEntityKind.objects.filter(short_name="ГИМС").first()
        if not kind and not dry_run:
            self.stdout.write(self.style.ERROR(
                "Нет kind ГИМС. Запусти миграцию 0031."
            ))
            return

        # 1) собираем кандидатов
        by_inn: dict[str, dict] = {}

        # Общий запрос (центральный ФКУ ЦОД + часть регионов)
        for base in ["ФКУ Центр ГИМС МЧС", "Центр ГИМС МЧС России"]:
            for s in dadata_suggest(base, token, count=20):
                if is_gims(s):
                    inn = (s.get("data") or {}).get("inn") or ""
                    if inn:
                        by_inn[inn] = s
            time.sleep(sleep_s)

        # Перебор по регионам
        for reg in REGIONS:
            q = f"ФКУ Центр ГИМС МЧС России по {reg}"
            for s in dadata_suggest(q, token, count=5):
                if is_gims(s):
                    inn = (s.get("data") or {}).get("inn") or ""
                    if inn:
                        by_inn[inn] = s
            time.sleep(sleep_s)

        self.stdout.write(f"Уникальных ГИМС найдено: {len(by_inn)}")

        # 2) запись
        created = updated = skipped = 0
        for inn, s in by_inn.items():
            data = s.get("data") or {}
            # Подтягиваем полные реквизиты через findById, если адрес/ОКПО не подтянулись
            full = dadata_find_by_inn(inn) or data
            enrichment = enrich_from_dadata(full)

            name = enrichment.get("name") or s.get("value") or ""
            short = enrichment.get("short_name") or name

            defaults = {
                "name": name,
                "short_name": short[:255],
                "brand": "",
                "kind": kind,
                "entity_type": map_entity_type(((full or {}).get("opf") or {}).get("short", "")) or "other",
                "status": "active",
                "is_active": True,
                "inn": (inn or "")[:12],
                "ogrn": (enrichment.get("ogrn") or full.get("ogrn") or "")[:15],
                "kpp": enrichment.get("kpp", ""),
                "okpo": enrichment.get("okpo", ""),
                "okved": enrichment.get("okved", ""),
                "legal_address": enrichment.get("legal_address", ""),
                "phone": enrichment.get("phone", ""),
                "email": enrichment.get("email", ""),
                "director_name": "",
                "director_title": "",
                "notes": "Импорт ФКУ Центр ГИМС МЧС России (DaData suggest)",
            }

            self.stdout.write(f"  ИНН={inn} → {name[:80]}")

            if dry_run:
                continue

            obj, is_new = LegalEntity.objects.update_or_create(
                inn=inn,
                defaults=defaults,
            )
            if is_new:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано: {created}, обновлено: {updated}, пропущено: {skipped}"
        ))
