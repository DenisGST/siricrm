"""
Импорт кредитных организаций со страницы ЦБ РФ в таблицу LegalEntity.
Источник: https://www.cbr.ru/banking_sector/credit/FullCoList/

Дополнительно обогащает записи данными из DaData по ОГРН:
ИНН, КПП, ОКПО, ОКВЭД, руководитель, адреса, телефоны, e-mail.
"""
import re
import urllib.request
from html.parser import HTMLParser

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.crm.models import LegalEntity, LegalEntityKind


CBR_URL = "https://www.cbr.ru/banking_sector/credit/FullCoList/"
DADATA_FIND_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"


def dadata_find_by_ogrn(ogrn: str) -> dict | None:
    """Возвращает data-блок первой организации из DaData по ОГРН."""
    token = getattr(settings, "DADATA_API_KEY", "") or ""
    if not token or not ogrn:
        return None
    try:
        r = requests.post(
            DADATA_FIND_URL,
            json={"query": ogrn, "count": 1},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {token}",
            },
            timeout=15,
        )
        r.raise_for_status()
        suggestions = r.json().get("suggestions") or []
        if not suggestions:
            return None
        return suggestions[0].get("data") or None
    except Exception:
        return None


def enrich_from_dadata(data: dict) -> dict:
    """Преобразует DaData-data в словарь полей LegalEntity."""
    if not data:
        return {}

    name_block = data.get("name") or {}
    full_name = (name_block.get("full_with_opf")
                 or name_block.get("full")
                 or "")
    short_name = (name_block.get("short_with_opf")
                  or name_block.get("short")
                  or "")

    mgmt = data.get("management") or {}
    address = data.get("address") or {}
    address_value = address.get("unrestricted_value") or address.get("value") or ""

    phones = data.get("phones") or []
    phone_value = phones[0].get("value") if phones else ""

    emails = data.get("emails") or []
    email_value = emails[0].get("value") if emails else ""

    return {
        "name": full_name,
        "short_name": (short_name or full_name)[:255],
        "inn": (data.get("inn") or "")[:12],
        "kpp": (data.get("kpp") or "")[:9],
        "okpo": (data.get("okpo") or "")[:14],
        "okved": (data.get("okved") or "")[:10],
        "director_name": mgmt.get("name") or "",
        "director_title": mgmt.get("post") or "",
        "legal_address": address_value,
        "phone": (phone_value or "")[:20],
        "email": email_value,
    }


def map_entity_type(opf: str) -> str:
    opf_u = (opf or "").upper()
    if "НПАО" in opf_u or "АО" in opf_u.split() or opf_u.startswith("АО"):
        # НПАО — непубличное АО
        if "НПАО" in opf_u:
            return "ao"
        if "ПАО" in opf_u:
            return "pao"
        return "ao"
    if "ПАО" in opf_u:
        return "pao"
    if "ООО" in opf_u:
        return "ooo"
    if "ИП" in opf_u:
        return "ip"
    return "other"


def map_status(license_status: str) -> str:
    s = (license_status or "").strip().lower()
    if "действующ" in s:
        return "active"
    # «Ликвидация» = в процессе ликвидации
    if "ликвидац" in s and "ликвидирован" not in s:
        return "liquidation"
    # Отозванная / Аннулированная / Ликвидирована — ликвидирована
    if "отозван" in s or "аннулирован" in s or "ликвидирован" in s:
        return "liquidated"
    return "active"


class BanksTableParser(HTMLParser):
    """Вытаскивает первую <table class="data levels"> и её <tr>/<td>."""

    def __init__(self):
        super().__init__()
        self.in_target_table = False
        self.table_class_seen = False
        self.in_tbody = False
        self.in_tr = False
        self.in_td = False
        self.cur_row = []
        self.cur_cell_parts = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            cls = attrs.get("class", "")
            if "data" in cls and "levels" in cls and not self.in_target_table:
                self.in_target_table = True
        elif self.in_target_table and tag == "tbody":
            self.in_tbody = True
        elif self.in_tbody and tag == "tr":
            self.in_tr = True
            self.cur_row = []
        elif self.in_tr and tag == "td":
            self.in_td = True
            self.cur_cell_parts = []

    def handle_endtag(self, tag):
        if tag == "td" and self.in_td:
            cell = "".join(self.cur_cell_parts).strip()
            cell = re.sub(r"\s+", " ", cell)
            self.cur_row.append(cell)
            self.in_td = False
        elif tag == "tr" and self.in_tr:
            if self.cur_row:
                self.rows.append(self.cur_row)
            self.in_tr = False
        elif tag == "tbody" and self.in_tbody:
            self.in_tbody = False
        elif tag == "table" and self.in_target_table:
            self.in_target_table = False

    def handle_data(self, data):
        if self.in_td:
            self.cur_cell_parts.append(data)


class Command(BaseCommand):
    help = "Импорт банков из реестра ЦБ РФ в LegalEntity."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=5,
                            help="Сколько записей загрузить (default 5)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Ничего не писать в БД, только показать")
        parser.add_argument("--no-enrich", action="store_true",
                            help="Пропустить обращение к DaData")

    def handle(self, *args, **opts):
        limit = opts["limit"]
        dry_run = opts["dry_run"]
        no_enrich = opts["no_enrich"]
        bank_kind = LegalEntityKind.objects.filter(short_name="Банк").first()

        self.stdout.write(f"Загружаю {CBR_URL} …")
        req = urllib.request.Request(CBR_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        parser = BanksTableParser()
        parser.feed(html)
        rows = parser.rows[:limit]
        self.stdout.write(f"Найдено строк в таблице: {len(parser.rows)}, беру {len(rows)}")

        created = 0
        updated = 0
        for row in rows:
            # ожидаемые колонки: 0=№, 1=Вид, 2=Рег.№, 3=ОГРН, 4=Наименование,
            # 5=ОПФ, 6=Дата регистрации, 7=Статус лицензии, 8=Адрес
            if len(row) < 9:
                self.stdout.write(self.style.WARNING(f"Пропуск: {row}"))
                continue
            num, kind, regn, ogrn, name, opf, reg_date, license_status, address = row[:9]

            status = map_status(license_status)
            entity_type = map_entity_type(opf)

            notes = (
                f"Импорт из реестра ЦБ РФ\n"
                f"Рег. номер ЦБ: {regn}\n"
                f"Вид: {kind or '—'}\n"
                f"Дата регистрации: {reg_date}\n"
                f"Статус лицензии: {license_status}"
            )

            # Обогащение через DaData по ОГРН
            enrichment = {}
            if not no_enrich:
                dd = dadata_find_by_ogrn(ogrn)
                enrichment = enrich_from_dadata(dd)
                if enrichment:
                    self.stdout.write(
                        f"    DaData: ИНН={enrichment.get('inn','—')} "
                        f"КПП={enrichment.get('kpp','—')} "
                        f"Рук.={enrichment.get('director_name','—')[:40]}"
                    )
                else:
                    self.stdout.write(self.style.WARNING(
                        f"    DaData: нет данных по ОГРН {ogrn}"
                    ))

            self.stdout.write(
                f"  [{num}] {name} | ОГРН={ogrn} | ОПФ={opf} → {entity_type} "
                f"| {license_status} → {status}"
            )

            if dry_run:
                continue

            # Поля-кандидаты для записи: сначала CBR, потом перекрываем DaData
            defaults = {
                "name": name,
                "short_name": name[:255],
                "entity_type": entity_type,
                "legal_address": address,
                "status": status,
                "notes": notes,
                "is_active": status == "active",
            }
            if bank_kind:
                defaults["kind"] = bank_kind
            for k, v in enrichment.items():
                if v:
                    defaults[k] = v

            obj, is_new = LegalEntity.objects.update_or_create(
                ogrn=ogrn,
                defaults=defaults,
            )
            if is_new:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Готово. Создано: {created}, обновлено: {updated}"
        ))
