"""Импорт региональных управлений ФССП (У/ГУ ФССП России по субъекту) в LegalEntity.

Зачем: в справочнике есть только районные ОСП (вид «ФССП», ~2868 шт), а
информационное письмо в УФССП адресуется САМОМУ управлению субъекта. Их 85 —
по одному на регион, вид ЮЛ «УФССП».

🛑 Имя управления НЕ угадываем падежами и НЕ ищем по КЛАДР (наши Region.number
не совпадают с КЛАДР: Чечня 95→20, Крым 82→91, а Москва отдаёт межрегиональное
ГМУ). Точное имя в правильном падеже уже зашито в названиях самих ОСП, напр.
«Абыйское РОСП УФССП России по Республике Саха (Якутия)» → берём хвост
«УФССП России по Республике Саха (Якутия)» и по нему тянем реквизиты из DaData.

Реквизиты (ИНН/ОГРН/адрес/индекс) — DaData suggest/party. Валидация: в найденном
названии должно быть «ПРИСТАВ» (иначе DaData притащила что-то чужое).
Идемпотентность — по (вид «УФССП» + регион).

🛑 DaData: квота 10k/сутки на ключ, общая — гоняем ТОЛЬКО на dev, на прод
переносим dumpdata/loaddata (см. память external-api-dev-then-sync).

  python manage.py import_ufssp --dry-run     # ничего не пишет, только отчёт
  python manage.py import_ufssp --limit 5     # прогон на 5 регионах
  python manage.py import_ufssp               # полный (85 регионов, ~85 запросов)
"""
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.crm.models import LegalEntity, LegalEntityKind, Region

SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"

OSP_KIND = "ФССП"       # районные отделы (источник имени управления)
UFSSP_KIND = "УФССП"    # региональные управления (что импортируем)

# «Абыйское РОСП УФССП России по Республике Саха (Якутия)» → хвост с управлением.
UPRAVLENIE_RE = re.compile(r"((?:Г|)У?ФССП\s+России\s+по\s+.+)$", re.IGNORECASE)


def _headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {getattr(settings, 'DADATA_API_KEY', '')}",
    }


def _pick(sug):
    """Первый кандидат-приставы из ответа DaData (иначе None).

    Фильтр по «ПРИСТАВ» — защита от чужой организации; «ГМУ»/«СПЕЦИАЛИЗИРОВАН» —
    это межрегиональные управления (в Москве DaData отдаёт ГМУ первым), нам нужно
    территориальное управление субъекта.
    """
    for s in (sug or []):
        d = s.get("data") or {}
        name = d.get("name") or {}
        full = (name.get("full_with_opf") or s.get("value") or "")
        up = full.upper()
        if "ПРИСТАВ" not in up:
            continue
        if "ГМУ" in up or "СПЕЦИАЛИЗИРОВАН" in up or "МЕЖРЕГИОНАЛЬН" in up:
            continue
        addr = d.get("address") or {}
        addr_data = addr.get("data") or {}
        return {
            "name": full,
            "short_name": (name.get("short_with_opf") or "")[:50],
            "inn": d.get("inn") or "",
            "kpp": d.get("kpp") or "",
            "ogrn": d.get("ogrn") or "",
            "legal_address": addr.get("unrestricted_value") or addr.get("value") or "",
            "postal_code": addr_data.get("postal_code") or "",
        }
    return None


def _ask(body):
    try:
        r = requests.post(SUGGEST_URL, json=body, headers=_headers(), timeout=15)
        r.raise_for_status()
        return _pick(r.json().get("suggestions"))
    except Exception:  # noqa: BLE001
        return None


def _dadata_party(query, region_number):
    """Реквизиты управления. Три попытки, каждая следующая — если предыдущая пуста:

      1) по точному имени из ОСП («ГУФССП России по Краснодарскому краю»);
      2) то же с пробелом («ГУ ФССП…») — часть управлений зарегистрирована так;
      3) поиск «Управление ФССП» с фильтром по региону (КЛАДР) — добивает имена
         со спецсимволами («… – Кузбассу», «… и Чукотскому АО»).
    """
    for q in (query, query.replace("ГУФССП", "ГУ ФССП")):
        got = _ask({"query": q, "count": 3, "status": ["ACTIVE"]})
        if got:
            return got
        time.sleep(0.12)
    return _ask({
        "query": "Управление Федеральной службы судебных приставов",
        "count": 5, "status": ["ACTIVE"],
        "locations": [{"kladr_id": f"{region_number:02d}"}],
    })


def _upravlenie_name(region, osp_kind):
    """Имя управления региона — из названия любого его ОСП."""
    qs = LegalEntity.objects.filter(
        kind=osp_kind, region=region, is_active=True, name__icontains="ФССП России по")
    for e in qs[:5]:
        m = UPRAVLENIE_RE.search(e.name or "")
        if m:
            return m.group(1).strip()
    return ""


class Command(BaseCommand):
    help = "Импорт 85 региональных управлений ФССП (УФССП/ГУФССП) в LegalEntity"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="только отчёт")
        parser.add_argument("--limit", type=int, default=0, help="сколько регионов")

    def handle(self, *args, **opts):
        dry, limit = opts["dry_run"], opts["limit"]
        if not getattr(settings, "DADATA_API_KEY", ""):
            self.stderr.write(self.style.ERROR("DADATA_API_KEY не задан"))
            return

        osp_kind = LegalEntityKind.objects.filter(short_name=OSP_KIND).first()
        ufssp_kind = LegalEntityKind.objects.filter(short_name=UFSSP_KIND).first()
        if not (osp_kind and ufssp_kind):
            self.stderr.write(self.style.ERROR(
                f"Нет вида ЮЛ {OSP_KIND!r} или {UFSSP_KIND!r} — прогоните миграции."))
            return

        regions = (Region.objects
                   .filter(id__in=LegalEntity.objects.filter(kind=osp_kind, is_active=True)
                           .exclude(region=None).values("region"))
                   .order_by("number"))
        if limit:
            regions = regions[:limit]

        created = updated = skipped = 0
        no_name, no_dadata = [], []
        for reg in regions:
            uname = _upravlenie_name(reg, osp_kind)
            if not uname:
                no_name.append(f"{reg.number} {reg.name}")
                skipped += 1
                continue
            party = _dadata_party(uname, reg.number)
            time.sleep(0.12)  # вежливый троттл к DaData
            if not party:
                # Реквизитов нет — заводим хотя бы имя, адресат будет подбираться,
                # адрес юрист допишет вручную (или добьём позже повторным прогоном).
                party = {"name": uname, "short_name": uname[:50], "inn": "", "kpp": "",
                         "ogrn": "", "legal_address": "", "postal_code": ""}
                no_dadata.append(f"{reg.number} {reg.name}")
            if dry:
                self.stdout.write(
                    f"  [{reg.number:3}] {party['short_name'][:44]:46} "
                    f"ИНН={party['inn'] or '—':12} {party['legal_address'][:40]}")
                continue
            with transaction.atomic():
                obj, is_new = LegalEntity.objects.update_or_create(
                    kind=ufssp_kind, region=reg,
                    defaults={
                        "name": party["name"] or uname,
                        "short_name": party["short_name"] or uname[:50],
                        "inn": party["inn"], "kpp": party["kpp"], "ogrn": party["ogrn"],
                        "legal_address": party["legal_address"],
                        "postal_code": party["postal_code"],
                        "is_active": True,
                    },
                )
            created += int(is_new)
            updated += int(not is_new)
            self.stdout.write(
                f"  {'+' if is_new else '~'} [{reg.number:3}] {obj.short_name[:46]:48} "
                f"ИНН={obj.inn or '—'}")

        self.stdout.write(self.style.SUCCESS(
            f"Готово: создано {created}, обновлено {updated}, пропущено {skipped}"))
        if no_name:
            self.stdout.write(self.style.WARNING(
                f"Имя управления не извлеклось ({len(no_name)}): {', '.join(no_name)}"))
        if no_dadata:
            self.stdout.write(self.style.WARNING(
                f"DaData без реквизитов ({len(no_dadata)}): {', '.join(no_dadata)} "
                "— заведены с именем, адрес допишите вручную"))
