"""
Импорт муниципальных органов управления имуществом (ДМИ).

Варианты наименования:
  - Комитет по управлению имуществом (КУИ)
  - Комитет по управлению муниципальным имуществом (КУМИ)
  - Комитет имущественных отношений (КИО)
  - Комитет имущественных и земельных отношений (КИЗО)
  - Департамент имущественных отношений (ДИО)
  - Департамент имущественных и земельных отношений (ДИЗО)
  - Департамент муниципального имущества (ДМИ)
  - Управление имущественных отношений (УИО)
  - Управление муниципального имущества (УМИ)
  - Управление земельных и имущественных отношений (УЗИО)
  - Отдел имущественных отношений (ОИО) — для малых МО

Стратегия:
  1. Из базы уже импортированных территориальных юрлиц (МРЭО, ФНС, ЗАГС,
     ГИМС, ЛРР — итого 13 000+ записей) извлекаем уникальные муниципальные
     образования: (регион, название города/района).
  2. Для каждого MO делаем DaData suggest с запросами "<муни> имуществ"
     и "<муни> КУМИ".
  3. Фильтруем: в имени должен быть корень «имущ» / «изо», ОПФ не
     коммерческая, статус ACTIVE/LIQUIDATING.
  4. Дедуп по ИНН.
  5. Сохраняем LegalEntity(kind=ДМИ).
"""
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.crm.models import LegalEntity, LegalEntityKind, Region
from apps.crm.management.commands.assign_legal_entity_regions import (
    find_region_number,
)
from apps.crm.management.commands.import_cbr_banks import (
    enrich_from_dadata,
    map_entity_type,
)


SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"

COMMERCIAL_OPF = {"ООО", "АО", "ПАО", "ЗАО", "ИП", "ОАО", "НКО", "НП", "ОО", "ТСЖ"}

# В названии обязательно должен встречаться один из корней.
NAME_KEYWORDS_RE = re.compile(
    r"\b(ИМУЩ\w*|ЗЕМЕЛ\w*|КУМИ|КУМИЗО|КИИЗО|КИЗО|ДИЗО|УИЗО|УЗИО|КИО|КУИ|ДМИ|УМИ|ДИО|УИО|ДГИ|ГОРИМУЩ\w*|ЗЕМИМУЩ\w*)\b",
    re.IGNORECASE,
)

# Название должно описывать муниципальный/государственный орган.
MUNI_WORDS_RE = re.compile(
    r"\b(КОМИТЕТ|УПРАВЛЕНИЕ|ДЕПАРТАМЕНТ|ОТДЕЛ|МКУ|МБУ|МКП|"
    r"МУНИЦИПАЛЬН|АДМИНИСТРАЦ|ПАЛАТА|СЛУЖБА|"
    r"КУМИ|КУМИЗО|КИИЗО|КИЗО|ДИЗО|УИЗО|УЗИО|КИО|КУИ|ДМИ|УМИ|ДИО|УИО)\b",
    re.IGNORECASE,
)


def dadata_suggest(query: str, token: str, count: int = 10) -> list[dict]:
    try:
        r = requests.post(
            SUGGEST_URL,
            json={"query": query, "count": count, "type": "LEGAL"},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {token}",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("suggestions") or []
    except Exception:
        return []


def looks_like_dmi(sugg: dict) -> bool:
    """Проверяет, что suggestion похож на муниципальный ДМИ."""
    data = sugg.get("data") or {}
    name = (sugg.get("value") or "").upper()
    if not name:
        return False
    # ОПФ не коммерческая
    opf = ((data.get("opf") or {}).get("short") or "").upper()
    if opf in COMMERCIAL_OPF:
        return False
    # Статус: ACTIVE или LIQUIDATING
    state = (data.get("state") or {}).get("status") or ""
    if state in ("LIQUIDATED",):
        return False
    # Ключевые слова
    if not NAME_KEYWORDS_RE.search(name):
        return False
    if not MUNI_WORDS_RE.search(name):
        # Исключение: аббревиатуры типа "КУМИ Г. …" — уже матчат KEYWORDS.
        # Но MUNI_WORDS покрывает почти всё — оставим строгой.
        return False
    return True


def extract_municipalities() -> list[tuple[int, str]]:
    """Извлекает уникальные (region_id, название МО) из территориальных юрлиц."""
    kinds = LegalEntityKind.objects.filter(
        short_name__in=["МРЭО", "ФНС", "ЗАГС", "ГИМС", "ЛРР"]
    )
    qs = LegalEntity.objects.filter(kind__in=kinds).exclude(legal_address="")

    munis: set[tuple[int, str]] = set()
    city_re = re.compile(
        r"(?<![а-я])(?:г\.?\s+|город\s+)([А-ЯЁ][а-яё\-]+(?:[ -][А-ЯЁа-яё\-]+)?)"
    )
    raion_re = re.compile(
        r"([А-ЯЁ][а-яё\-]+ский)\s+р-н"
        r"|"
        r"([А-ЯЁ][а-яё\-]+ский)\s+район",
    )

    for le in qs.only("legal_address", "region_id").iterator(chunk_size=500):
        if not le.region_id:
            continue
        addr = le.legal_address
        for m in city_re.finditer(addr):
            city = m.group(1).strip()
            if len(city) >= 3:
                munis.add((le.region_id, city))
                break
        for m in raion_re.finditer(addr):
            raion = (m.group(1) or m.group(2) or "").strip()
            if raion and len(raion) >= 5:
                munis.add((le.region_id, raion.replace("ский", "ский район")))
    return sorted(munis)


class Command(BaseCommand):
    help = "Импорт муниципальных органов управления имуществом (ДМИ/КУМИ)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--sleep", type=float, default=0.12)
        parser.add_argument("--limit", type=int, default=0, help="Ограничить число МО")
        parser.add_argument("--region", type=int, default=0, help="Только один регион по Region.number")

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        sleep_s = opts["sleep"]
        limit = opts["limit"]
        only_region = opts["region"]

        token = getattr(settings, "DADATA_API_KEY", "") or ""
        if not token:
            self.stdout.write(self.style.ERROR("DADATA_API_KEY не задан"))
            return

        kind = LegalEntityKind.objects.filter(short_name="ДМИ").first()
        if not kind and not dry_run:
            self.stdout.write(self.style.ERROR(
                "Нет LegalEntityKind(ДМИ). Прогоните миграцию 0035."
            ))
            return

        munis = extract_municipalities()
        if only_region:
            region = Region.objects.filter(number=only_region).first()
            if region:
                munis = [m for m in munis if m[0] == region.id]
        if limit:
            munis = munis[:limit]

        self.stdout.write(f"МО для обработки: {len(munis)}")
        regions_by_id = {r.id: r for r in Region.objects.all()}

        # Собираем уникальных по ИНН кандидатов.
        by_inn: dict[str, dict] = {}

        for i, (rid, muni) in enumerate(munis, 1):
            rg = regions_by_id.get(rid)
            if not rg:
                continue

            queries = [
                f"{muni} имуществ",
                f"{muni} КУМИ",
            ]
            for q in queries:
                for s in dadata_suggest(q, token, count=10):
                    if not looks_like_dmi(s):
                        continue
                    inn = (s.get("data") or {}).get("inn")
                    if not inn:
                        continue
                    if inn in by_inn:
                        continue
                    # Проверяем регион по адресу suggestion — должен совпадать
                    # с тем регионом, по которому мы ищем (муниципалитет).
                    data = s.get("data") or {}
                    addr = (data.get("address") or {}).get("unrestricted_value") or ""
                    addr_region = find_region_number(addr)
                    if addr_region and addr_region != rg.number:
                        continue
                    by_inn[inn] = {"sugg": s, "region": rg, "muni": muni}
                time.sleep(sleep_s)

            if i % 50 == 0:
                self.stdout.write(f"  [{i}/{len(munis)}]  уникальных ДМИ: {len(by_inn)}")

        self.stdout.write(f"Всего уникальных ДМИ: {len(by_inn)}")

        # Сохраняем.
        created = updated = 0
        with transaction.atomic():
            for inn, payload in by_inn.items():
                s = payload["sugg"]
                rg = payload["region"]
                muni = payload["muni"]
                data = s.get("data") or {}

                full_name = ((data.get("name") or {}).get("full_with_opf")
                             or s.get("value") or "")
                short_name = ((data.get("name") or {}).get("short_with_opf")
                              or s.get("value") or "")[:255]

                enrichment = enrich_from_dadata(data)
                legal_address = enrichment.get("legal_address") or ""

                # Уточняем регион по адресу (если DaData даёт другой — верим ему).
                rgn_num = find_region_number(legal_address) or rg.number
                region_obj = regions_by_id.get(rg.id)
                if rgn_num != rg.number:
                    rg_alt = Region.objects.filter(number=rgn_num).first()
                    if rg_alt:
                        region_obj = rg_alt

                defaults = {
                    "name": full_name[:500],
                    "short_name": short_name,
                    "kind": kind,
                    "entity_type": map_entity_type(
                        ((data.get("opf") or {}).get("short") or "")
                    ) or "other",
                    "status": "active",
                    "is_active": True,
                    "inn": (inn or "")[:12],
                    "ogrn": (data.get("ogrn") or "")[:15],
                    "kpp": enrichment.get("kpp", ""),
                    "okpo": enrichment.get("okpo", ""),
                    "okved": enrichment.get("okved", ""),
                    "legal_address": legal_address,
                    "phone": enrichment.get("phone", "")[:20],
                    "email": enrichment.get("email", "")[:100],
                    "director_name": enrichment.get("director_name", "")[:255],
                    "director_title": enrichment.get("director_title", "")[:255],
                    "region": region_obj,
                    "notes": (
                        "Импорт через DaData suggest "
                        f"(поиск по МО: {rg.name} / {muni})"
                    ),
                }

                if dry_run:
                    self.stdout.write(
                        f"  [{rg.number:3}] ИНН={inn} | {short_name[:80]}"
                    )
                    continue

                obj, is_new = LegalEntity.objects.update_or_create(
                    inn=inn, kind=kind,
                    defaults=defaults,
                )
                if is_new:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано: {created}, обновлено: {updated}"
        ))
