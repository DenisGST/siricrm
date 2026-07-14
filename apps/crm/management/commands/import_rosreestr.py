"""Импорт региональных управлений Росреестра в LegalEntity.

Адресат «Информационного письма в Росреестр» — Управление Росреестра по субъекту
(по образцу юриста: «Управление Росреестра по Ставропольскому краю, 355012,
г. Ставрополь, ул. Комсомольская, 58»). Их 85 — по одному на регион, вид «Росреестр».

🛑 Падеж региона не угадываем и КЛАДР не используем (наши Region.number не
совпадают с КЛАДР: Чечня 95→20, Крым 82→91). Берём готовую падежную форму
«по <региону>» из названий управлений ФССП (импортированы командой import_ufssp,
там падеж уже правильный) и подставляем в «Управление Росреестра по <региону>».
Реквизиты — DaData suggest/party, валидация по «РОСРЕЕСТР»/«РЕГИСТРАЦИИ» в имени.

Идемпотентность — по (вид «Росреестр» + регион).

🛑 DaData: квота 10k/сутки, общая на ключ — гоняем ТОЛЬКО на dev, на прод
переносим сид-файлом (см. память external-api-dev-then-sync).

  python manage.py import_rosreestr --dry-run
  python manage.py import_rosreestr
"""
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.crm.models import LegalEntity, LegalEntityKind, Region

SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"

UFSSP_KIND = "УФССП"        # источник падежной формы «по <региону>»
ROSREESTR_KIND = "Росреестр"

SUFFIX_RE = re.compile(r"(по\s+.+)$", re.IGNORECASE)


def _headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {getattr(settings, 'DADATA_API_KEY', '')}",
    }


def _pick(sug):
    for s in (sug or []):
        d = s.get("data") or {}
        name = d.get("name") or {}
        full = (name.get("full_with_opf") or s.get("value") or "")
        up = full.upper()
        if "РОСРЕЕСТР" not in up and "РЕГИСТРАЦИИ" not in up:
            continue
        # Центральный аппарат Росреестра (без «по <региону>») нам не нужен.
        if "УПРАВЛЕНИЕ" not in up:
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


def _ask(query):
    try:
        r = requests.post(
            SUGGEST_URL, json={"query": query, "count": 5, "status": ["ACTIVE"]},
            headers=_headers(), timeout=15,
        )
        r.raise_for_status()
        return _pick(r.json().get("suggestions"))
    except Exception:  # noqa: BLE001
        return None


def _queries(suffix):
    """Варианты запроса из падежного суффикса ФССП.

    У ФССП часть управлений объединяют субъекты, а у Росреестра они раздельные:
      «по Хабаровскому краю И Еврейской автономной области» → «по Хабаровскому краю»
      «по Чувашской Республике - Чувашии»                   → «по Чувашской Республике»
      «по Республике Крым и г. Севастополю»                 → «по Республике Крым»
      «по г. Москве»                                        → «по Москве»
    Пробуем по очереди, пока DaData не отдаст управление Росреестра.
    """
    variants = [suffix]
    # Отрезаем присоединённый субъект: «… и <субъект>» / «… - <субъект>».
    for pat in (r"\s+и\s+", r"\s+[-–—]\s+"):
        cut = re.split(pat, suffix, flags=re.IGNORECASE)[0].strip()
        if cut != suffix and cut not in variants:
            variants.append(cut)
    # «по г. Москве» → «по Москве» (в т.ч. для уже обрезанных вариантов).
    for v in list(variants):
        no_g = re.sub(r"\bг\.\s*", "", v, flags=re.IGNORECASE).strip()
        if no_g not in variants:
            variants.append(no_g)
    return [f"Управление Росреестра {v}" for v in variants]


class Command(BaseCommand):
    help = "Импорт региональных управлений Росреестра в LegalEntity"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, **opts):
        dry, limit = opts["dry_run"], opts["limit"]
        if not getattr(settings, "DADATA_API_KEY", ""):
            self.stderr.write(self.style.ERROR("DADATA_API_KEY не задан"))
            return

        ufssp_kind = LegalEntityKind.objects.filter(short_name=UFSSP_KIND).first()
        if not ufssp_kind:
            self.stderr.write(self.style.ERROR(
                "Нет вида «УФССП» — сначала прогоните import_ufssp (нужен для падежей)."))
            return
        rr_kind, _ = LegalEntityKind.objects.get_or_create(
            name="Управление Росреестра по субъекту РФ",
            defaults={"short_name": ROSREESTR_KIND},
        )

        ufssp = (LegalEntity.objects.filter(kind=ufssp_kind, is_active=True)
                 .exclude(region=None).select_related("region").order_by("region__number"))
        if limit:
            ufssp = ufssp[:limit]

        created = updated = 0
        missing = []
        for u in ufssp:
            reg = u.region
            m = SUFFIX_RE.search(u.name or "")
            if not m:
                missing.append(f"{reg.number} {reg.name}")
                continue
            suffix = m.group(1).strip()
            party = None
            for q in _queries(suffix):
                party = _ask(q)
                time.sleep(0.12)
                if party:
                    break
            if not party:
                missing.append(f"{reg.number} {reg.name}")
                continue
            if dry:
                self.stdout.write(
                    f"  [{reg.number:3}] {party['short_name'][:44]:46} "
                    f"ИНН={party['inn'] or '—':12} {party['legal_address'][:38]}")
                continue
            with transaction.atomic():
                obj, is_new = LegalEntity.objects.update_or_create(
                    kind=rr_kind, region=reg,
                    defaults={
                        "name": party["name"], "short_name": party["short_name"],
                        "inn": party["inn"], "kpp": party["kpp"], "ogrn": party["ogrn"],
                        "legal_address": party["legal_address"],
                        "postal_code": party["postal_code"],
                        "is_active": True,
                    },
                )
            created += int(is_new)
            updated += int(not is_new)
            self.stdout.write(
                f"  {'+' if is_new else '~'} [{reg.number:3}] {obj.short_name[:46]:48} ИНН={obj.inn or '—'}")

        self.stdout.write(self.style.SUCCESS(
            f"Готово: создано {created}, обновлено {updated}"))
        if missing:
            self.stdout.write(self.style.WARNING(
                f"Не найдено ({len(missing)}): {', '.join(missing)}"))
