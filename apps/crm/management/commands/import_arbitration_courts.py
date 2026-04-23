"""
Импорт адресов и реквизитов арбитражных судов субъектов РФ
из официального JSON портала arbitr.ru.

Источник: https://arbitr.ru/ac/map
Структура: топ-уровень — 10 федеральных арбитражных округов, внутри
поля `ac1` — суды субъектов РФ.

Для каждого суда субъекта:
  1. Определяем Region по названию (s_name — например «Свердловской области»).
  2. Создаём (или обновляем) Address с разобранным адресом.
  3. Привязываем Region.court_address = Address.
  4. Обновляем Region.court_name и Region.court_payment_details.
"""
import re
import requests

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.crm.models import Address, Region


API_URL = "https://arbitr.ru/ac/map"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# Соответствие "s_name арбитражного суда" → Region.number.
# s_name выглядит как "Свердловской области", "Республики Татарстан",
# "г. Москвы", "Чукотского автономного округа" и т. п.
# Для надёжности используем регэкс-паттерны (аналогично assign_legal_entity_regions).
REGION_PATTERNS: list[tuple[str, int]] = [
    (r"ханты[\s\-]*мансийск", 86),
    (r"ямало[\s\-]*ненец", 89),
    (r"кабардино[\s\-]*балкар", 7),
    (r"карачаево[\s\-]*черкес", 9),
    (r"\bсанкт[\s\-]*петербург", 78),
    (r"\bленинградск", 47),            # Арб. суд СПб и ЛО — отдельно мапим и на 78, и 47
    (r"\bненецк\w* автоном", 83),
    (r"\bчукотск", 87),
    (r"\bеврейск", 79),
    (r"\bсахалинск", 65),
    (r"\bсах(а|алин)\w*\s*\(якут", 14),
    (r"\bякут", 14),
    (r"северн\w*\s+осет", 15),
    (r"\bчеченск", 95),
    (r"\bчувашск", 21),
    (r"\bудмуртск", 18),
    (r"\bадыгея", 1),
    (r"\bбашкорт", 2),
    (r"\bбурят", 3),
    (r"\bреспублик\w*\s+алтай", 4),
    (r"\bдагестан", 5),
    (r"\bингушет", 6),
    (r"\bкалмык", 8),
    (r"\bкарели", 10),
    (r"\bреспублик\w*\s+коми", 11),
    (r"\bмарий\s*эл", 12),
    (r"\bмордов", 13),
    (r"\bтатарстан", 16),
    (r"\bтыва", 17),
    (r"\bхакас", 19),
    (r"\bкрым", 82),
    (r"\bсевастопол", 92),
    (r"\bдонецк\w* народн", 80),
    (r"\bлуганск\w* народн", 81),
    (r"\bзапорожск", 85),
    (r"\bалтайск\w*\s+кра", 22),
    (r"\bкраснодарск", 23),
    (r"\bкрасноярск", 24),
    (r"\bприморск", 25),
    (r"\bставропольск", 26),
    (r"\bхабаровск", 27),
    (r"\bпермск", 59),
    (r"\bкамчатск", 41),
    (r"\bзабайкальск", 75),
    (r"\bамурск", 28),
    (r"\bархангельск", 29),
    (r"\bастрахан", 30),
    (r"\bбелгородск", 31),
    (r"\bбрянск", 32),
    (r"\bвладимирск", 33),
    (r"\bволгоград", 34),
    (r"\bвологодск", 35),
    (r"\bворонеж", 36),
    (r"\bиванов", 37),
    (r"\bиркутск", 38),
    (r"\bкалининградск", 39),
    (r"\bкалуж", 40),
    (r"\bкемеровск", 42),
    (r"\bкиров\w* обл", 43),
    (r"\bкостромск", 44),
    (r"\bкурганск", 45),
    (r"\bкурск", 46),
    (r"\bлипецк", 48),
    (r"\bмагаданск", 49),
    (r"\bмурманск", 51),
    (r"\bнижегородск", 52),
    (r"\bновгородск", 53),
    (r"\bнижний\s+новгород", 52),
    (r"\bновосибирск", 54),
    (r"\bомск", 55),
    (r"\bоренбург", 56),
    (r"\bорловск", 57),
    (r"\bпензенск", 58),
    (r"\bпсковск", 60),
    (r"\bростовск", 61),
    (r"\bрязан", 62),
    (r"\bсамарск", 63),
    (r"\bсаратов", 64),
    (r"\bсвердловск", 66),
    (r"\bсмоленск", 67),
    (r"\bтамбовск", 68),
    (r"\bтвер", 69),
    (r"\bтомск", 70),
    (r"\bтульск", 71),
    (r"\bтюмен", 72),
    (r"\bульяновск", 73),
    (r"\bчелябинск", 74),
    (r"\bярослав", 76),
    (r"\bмосков\w* обл", 50),       # обл до г. Москвы
    (r"\bг\.?\s*москв", 77),
    (r"\bмосквы?\b", 77),
]


def find_region(s_name: str) -> int | None:
    s = (s_name or "").lower().replace("ё", "е")
    for pat, num in REGION_PATTERNS:
        if re.search(pat, s):
            return num
    return None


# Арбитражные суды, юрисдикция которых включает несколько субъектов.
# Один и тот же court_address будет привязан ко всем перечисленным регионам.
EXTRA_REGIONS: dict[int, list[int]] = {
    78: [47],   # СПб и Ленинградская область
    29: [83],   # Архангельская область и НАО
}


def normalize_address(address1: str, address2: str) -> tuple[str, str, str]:
    """Из (address1, address2) — делаем (полный адрес, индекс, город)."""
    parts = []
    if address1:
        parts.append(address1.strip(" ,"))
    if address2:
        parts.append(address2.strip(" ,"))
    full = ", ".join(parts)
    full = re.sub(r"\s+", " ", full).strip(" ,")

    postal = ""
    m = re.search(r"\b(\d{6})\b", full)
    if m:
        postal = m.group(1)

    # Город — обычно идёт после индекса и до запятой: "603082 Нижний Новгород,"
    city = ""
    m = re.search(r"\d{6}\s+([А-ЯЁ][а-яё\-]+(?:[\s\-][А-ЯЁа-яё\-]+){0,3})", full)
    if m:
        city = m.group(1).strip()
    return full, postal, city


class Command(BaseCommand):
    help = "Импорт адресов и реквизитов арбитражных судов субъектов РФ"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]

        self.stdout.write(f"Скачиваю {API_URL} …")
        r = requests.get(API_URL, headers=HEADERS, timeout=30, verify=False)
        r.raise_for_status()
        data = r.json()
        self.stdout.write(f"  {len(data)} округов")

        # Собираем плоский список судов-субъектов из ac1.
        subject_courts = []
        for okrug in data:
            for sub in (okrug.get("ac1") or []):
                subject_courts.append(sub)
        self.stdout.write(f"  судов субъектов: {len(subject_courts)}")

        regions_by_num = {r.number: r for r in Region.objects.all()}
        matched = unmatched = updated = created_addr = 0

        with transaction.atomic():
            for court in subject_courts:
                s_name = court.get("s_name") or ""
                name = court.get("name") or ""
                num = find_region(s_name) or find_region(name)
                region = regions_by_num.get(num) if num else None

                if not region:
                    unmatched += 1
                    self.stdout.write(self.style.WARNING(
                        f"  не нашёл регион: {name[:70]}"
                    ))
                    continue
                matched += 1

                full, postal, city = normalize_address(
                    court.get("address1") or "", court.get("address2") or "",
                )

                notes_lines = []
                if court.get("chief"):
                    notes_lines.append(f"Председатель: {court['chief']}")
                if court.get("www") or court.get("url"):
                    notes_lines.append(f"Сайт: {court.get('www') or court.get('url')}")
                if court.get("email_subscr"):
                    notes_lines.append(f"Email: {court['email_subscr']}")
                comment = "\n".join(notes_lines)[:500]

                if dry_run:
                    self.stdout.write(
                        f"  {region.number:3} {region.name[:30]:30} "
                        f"| {full[:90]}"
                    )
                    continue

                # Создаём/обновляем Address без клиента.
                addr = region.court_address
                if addr is None:
                    addr = Address.objects.create(
                        client=None,
                        address_type="default",
                        source=full,
                        result=full,
                        postal_code=postal,
                        country="Россия",
                        city=city,
                        comment=comment,
                    )
                    created_addr += 1
                else:
                    addr.source = full
                    addr.result = full
                    addr.postal_code = postal
                    addr.city = city
                    addr.comment = comment
                    addr.save()

                # Реквизиты госпошлины
                details_lines = []
                for key, label in [
                    ("recipient", "Получатель"),
                    ("inn", "ИНН"),
                    ("kpp", "КПП"),
                    ("bik", "БИК"),
                    ("account", "Р/с"),
                    ("bank", "Банк"),
                    ("kbk", "КБК"),
                    ("okato", "ОКАТО"),
                ]:
                    v = court.get(key)
                    if v:
                        details_lines.append(f"{label}: {v}")
                payment_text = "\n".join(details_lines)

                # Основной регион и «сателлиты» (НАО, Ленобласть и т. д.).
                target_numbers = [region.number] + EXTRA_REGIONS.get(region.number, [])
                for tn in target_numbers:
                    tr = regions_by_num.get(tn)
                    if not tr:
                        continue
                    tr.court_address = addr
                    tr.court_name = name
                    tr.court_payment_details = payment_text
                    tr.save(update_fields=[
                        "court_address", "court_name", "court_payment_details",
                    ])
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nИтого: сопоставлено {matched}, не сопоставлено {unmatched}, "
            f"создано адресов {created_addr}, обновлено регионов {updated}"
        ))
