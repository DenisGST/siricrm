"""
Импорт саморегулируемых организаций арбитражных управляющих (СРО АУ).

Источник списка: https://reestrbankrotov.ru/reestr-sro-arbitrazhnyh-upravlyayuschih/
(агрегатор, собирает данные из госреестра СРО, который ведёт Росреестр)

Для каждой СРО берём наименование → поиск в DaData по имени (suggest/party)
→ берём первую организацию-НКО (АССОЦИАЦИЯ / СОЮЗ / НП) → findById по ИНН.
"""
import html as htmlmod
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind
from apps.crm.management.commands.import_cbr_banks import (
    dadata_find_by_ogrn,
    enrich_from_dadata,
    map_entity_type,
)
from apps.crm.management.commands.import_myfin_mfo import dadata_find_by_inn


SRC_URL = "https://reestrbankrotov.ru/reestr-sro-arbitrazhnyh-upravlyayuschih/"
DADATA_SUGGEST_URL = (
    "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}


def dadata_suggest_party(query: str, count: int = 5) -> list[dict]:
    token = getattr(settings, "DADATA_API_KEY", "") or ""
    if not token:
        return []
    try:
        r = requests.post(
            DADATA_SUGGEST_URL,
            json={"query": query, "count": count},
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


def parse_source(html: str) -> list[dict]:
    """Возвращает [{reg_number, name, members_count}, ...]."""
    tbl_start = html.find("<table")
    tbl_end = html.find("</table>", tbl_start)
    if tbl_start == -1:
        return []
    tbl = html[tbl_start:tbl_end]
    rows = re.findall(r"<tr>.*?</tr>", tbl, re.DOTALL)
    items = []
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) < 3:
            continue
        reg = re.sub(r"<[^>]+>", "", cells[0]).strip()
        name = htmlmod.unescape(re.sub(r"<[^>]+>", "", cells[1]).strip())
        members = re.sub(r"<[^>]+>", "", cells[2]).strip()
        if not name or not reg:
            continue
        items.append({"reg_number": reg, "name": name, "members_count": members})
    return items


def pick_best_suggestion(suggestions: list[dict], hint: str) -> dict | None:
    """Из вариантов DaData выбираем НКО (ассоциация/союз/НП)."""
    if not suggestions:
        return None
    hint_l = hint.lower()
    # 1) ищем точное вхождение подстроки названия
    for s in suggestions:
        nm = (s.get("value") or "").lower()
        if any(w in nm for w in ("ассоциац", "союз", "некоммерческ", "партнёр", "партнер")):
            # и что-то из оригинального названия тоже содержится
            key_words = [w for w in hint_l.split() if len(w) > 4]
            if any(kw in nm for kw in key_words[:5]):
                return s
    # 2) fallback: первое
    return suggestions[0]


class Command(BaseCommand):
    help = "Импорт СРО арбитражных управляющих в LegalEntity"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-enrich", action="store_true")
        parser.add_argument("--sleep", type=float, default=0.2)

    def handle(self, *args, **opts):
        limit = opts["limit"]
        dry_run = opts["dry_run"]
        no_enrich = opts["no_enrich"]
        sleep_s = opts["sleep"]

        kind = LegalEntityKind.objects.filter(short_name="СРО").first()
        if not kind and not dry_run:
            self.stdout.write(self.style.ERROR(
                "Нет LegalEntityKind(СРО). Запусти миграцию 0027."
            ))
            return

        self.stdout.write(f"Загружаю {SRC_URL} …")
        r = requests.get(SRC_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
        items = parse_source(html)
        if limit:
            items = items[:limit]
        self.stdout.write(f"  найдено СРО: {len(items)}")

        created = updated = skipped = 0
        for idx, it in enumerate(items, 1):
            name = it["name"]

            enrichment = {}
            inn = ""
            ogrn = ""
            if not no_enrich:
                sugs = dadata_suggest_party(name, count=5)
                best = pick_best_suggestion(sugs, name)
                if best:
                    data = best.get("data") or {}
                    inn = (data.get("inn") or "")[:12]
                    ogrn = (data.get("ogrn") or "")[:15]
                    # подтягиваем полные реквизиты через findById
                    if inn:
                        dd = dadata_find_by_inn(inn)
                    else:
                        dd = None
                    if not dd:
                        dd = dadata_find_by_ogrn(ogrn) if ogrn else None
                    enrichment = enrich_from_dadata(dd) if dd else enrich_from_dadata(data)

            if not inn and not enrichment.get("inn"):
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f"  [{it['reg_number']}] ПРОПУСК (DaData не нашла): {name[:70]}"
                ))
                continue

            inn = inn or enrichment.get("inn", "")
            ogrn = ogrn or enrichment.get("ogrn", "")

            defaults = {
                "name": enrichment.get("name") or name,
                "short_name": (enrichment.get("short_name") or name)[:255],
                "brand": "",
                "kind": kind,
                "entity_type": map_entity_type(((enrichment or {}).get("entity_type", "") or "")) or "other",
                "status": "active",
                "is_active": True,
                "inn": inn[:12],
                "ogrn": ogrn[:15],
                "kpp": enrichment.get("kpp", ""),
                "okpo": enrichment.get("okpo", ""),
                "okved": enrichment.get("okved", ""),
                "legal_address": enrichment.get("legal_address", ""),
                "phone": enrichment.get("phone", ""),
                "email": enrichment.get("email", ""),
                "director_name": "",
                "director_title": "",
                "notes": (
                    f"Импорт из реестра СРО АУ (reestrbankrotov.ru)\n"
                    f"Рег. № в госреестре: {it['reg_number']}\n"
                    f"Членов СРО (на момент импорта): {it['members_count']}\n"
                    f"Первичное название: {name}"
                ),
            }

            self.stdout.write(
                f"  [{it['reg_number']}] {defaults['name'][:60]} | ИНН={inn}"
            )

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

            if sleep_s:
                time.sleep(sleep_s)

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано: {created}, обновлено: {updated}, пропущено: {skipped}"
        ))
