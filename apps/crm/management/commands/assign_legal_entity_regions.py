"""
Назначает поле `region` у LegalEntity на основе адресов.

Стратегия:
1. Для каждого юрлица собираем склеенную строку из
   legal_address / actual_address / postal_address.
2. Нормализуем (lowercase, ё→е, схлопываем пробелы).
3. Ищем в строке первый подходящий паттерн из REGION_PATTERNS.
4. Если не нашли — пробуем CITY_ALIASES (города → регион).
5. Если и это не помогло — region остаётся пустым.

Паттерны упорядочены от специфичных к общим, чтобы
"Кабардино-Балкарская" срабатывала раньше "Балкарск", а
"Ханты-Мансийский" — раньше "Мансийский".
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.crm.models import LegalEntity, Region


# (pattern, region.number) — первое совпадение побеждает
REGION_PATTERNS: list[tuple[str, int]] = [
    # Составные названия (должны идти первыми)
    (r"ханты[\s\-]*мансийск", 86),
    (r"хмао", 86),
    (r"ямало[\s\-]*ненец", 89),
    (r"янао", 89),
    (r"\bкабардино[- ]?балкар", 7),
    (r"\bкбр\b", 7),
    (r"\bкарачаево[- ]?черкес", 9),
    (r"\bсанкт[- ]?петербург", 78),
    (r"\bненецк", 83),
    (r"\bчукотск\w* автоном", 87),
    (r"\bчукотский\s+ао", 87),
    (r"\bеврейск", 79),
    (r"\bаобл\s+еврейск", 79),
    (r"\bреспублика\s+саха", 14),
    (r"\bресп\w*\s+саха", 14),
    (r"\b(саха)\s*\(якут", 14),
    (r"\bякут", 14),
    (r"\bосет", 15),
    (r"\bчеченск", 95),
    (r"\bчр\b", 95),
    (r"\bчувашск", 21),
    (r"\bчуваш", 21),
    (r"\bудмуртск", 18),
    (r"\bудмурт", 18),
    (r"\bудмурстк", 18),         # опечатка
    # Республики
    (r"\bадыгея", 1),
    (r"\bбашкорт", 2),
    (r"\bрб\b", 2),
    (r"\bбурят", 3),
    (r"\bреспублика алт?ай", 4),      # "Алтай"/"Алай" (опечатки)
    (r"\bресп\w* алт?ай", 4),
    (r"\bреспуб?лика алт?ай", 4),     # "Република Алтай"
    (r"\bрепублика алт?ай", 4),       # "Република" (без "с")
    (r"\bалтай\s+респ", 4),
    (r"\bдагестан", 5),
    (r"\bрд\b", 5),
    (r"\bингушет", 6),
    (r"\bкалмык", 8),
    (r"\bкарели", 10),
    (r"\bресп\w* коми", 11),
    (r"\bреспублика коми", 11),
    (r"\bрес\s*публика\s*коми", 11),   # опечатки типа "Рес публика"
    (r"\bкоми\s+респ", 11),
    (r"\bрк\b", 11),                    # "РК, Прилузский район" — в адресах практически только Коми
    (r"\bреспублика коми", 11),
    (r"\bмарий\s*эл", 12),
    (r"\bмордов", 13),
    (r"\bтатарстан", 16),
    (r"\bрт\b", 16),
    (r"\bтыва", 17),
    (r"\bтува", 17),
    (r"\bхакас", 19),
    (r"\bкрым", 82),
    (r"\bднр\b|донецк\w* народн", 80),
    (r"\bлнр\b|луганск\w* народн", 81),
    (r"\bзапорожск", 85),
    # Края
    (r"\bалтайск\w* край", 22),
    (r"\bкраснодар", 23),
    (r"\bкрасноярск", 24),
    (r"\bприморск", 25),
    (r"\bставрополь", 26),
    (r"\bставропольск", 26),
    (r"\bставоропль", 26),     # опечатка
    (r"\bставропольксий", 26), # опечатка
    (r"\bхабаровск", 27),
    (r"\bпермск", 59),
    (r"\bкамчатск", 41),
    (r"\bзабайкальск", 75),
    (r"\bчитинск", 75),       # старое название Забайкальского края
    (r"\bчита\b", 75),         # г. Чита
    # Области
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
    (r"\bкалуг", 40),
    (r"\bкемеровск", 42),
    (r"\bкировск\w*\s+(обл|область)", 43),
    (r"\bг\.?\s*киров\b", 43),
    (r"\bкиров\s+г\b", 43),
    (r"\bкостромск", 44),
    (r"\bкурган", 45),
    (r"\bкурск", 46),
    (r"\bленинградск", 47),
    (r"\bлипецк", 48),
    (r"\bмагаданск", 49),
    (r"\bмосковск", 50),
    (r"\bмо\b", 50),  # "МО" — редкая аббревиатура, но встречается
    (r"\bмурманск", 51),
    (r"\bнижегородск", 52),
    (r"\bновгородск", 53),
    (r"\bновосибирск", 54),
    (r"\bомск", 55),
    (r"\bоренбург", 56),
    (r"\bорловск", 57),
    (r"\bпенз", 58),
    (r"\bпезенск", 58),        # опечатка "Пезенская область"
    (r"\bпсковск", 60),
    (r"\bростовск", 61),
    (r"\bрязан", 62),
    (r"\bсамарск", 63),
    (r"\bсаратов", 64),
    (r"\bсахалинск", 65),
    (r"\bсвердловск", 66),
    (r"\bсмоленск", 67),
    (r"\bтамбовск", 68),
    (r"\bтвер", 69),
    (r"\bтомск", 70),
    (r"\bтульск", 71),
    (r"\bтула\b", 71),
    (r"\bг\.?\s*тула", 71),
    (r"\bтюмен", 72),
    (r"\bульяновск", 73),
    (r"\bчелябинск", 74),
    (r"\bярослав", 76),
    # Города федерального значения (ПОСЛЕ "московск", иначе области ловятся)
    (r"\bсевастополь", 92),
    (r"\bг\.?\s*москва\b", 77),
    (r"\bмосква\b", 77),
    (r"\bзеленоград\b", 77),   # САО Москвы
]


# Города, которые однозначно указывают на регион, но сам регион не всегда
# присутствует в строке (например, "420094, Казань г, ..." без "Татарстан").
# Применяем только если REGION_PATTERNS ничего не нашли.
CITY_ALIASES: list[tuple[str, int]] = [
    (r"\bказан", 16),                # Казань → Татарстан
    (r"\bнижний\s+новгород", 52),
    (r"\bниж\.?\s*новгород", 52),
    (r"\bгрозн", 95),                # Грозный → Чечня
    (r"\bюжно[- ]?сахалинск", 65),
    (r"\bростов[- ]?на[- ]?дону", 61),
    (r"\bмахачкала", 5),
    (r"\bнальчик", 7),               # → КБР
    (r"\bчеркесск", 9),              # → КЧР
    (r"\bвладикавказ", 15),
    (r"\bулан[- ]?удэ", 3),          # → Бурятия
    (r"\bчебоксар", 21),             # → Чувашия
    (r"\bйошкар[- ]?ола", 12),       # → Марий Эл
    (r"\bсаранск", 13),              # → Мордовия
    (r"\bсаранске", 13),
    (r"\bкызыл", 17),                # → Тыва
    (r"\bабакан", 19),               # → Хакасия
    (r"\bмайкоп", 1),                # → Адыгея
    (r"\bуфа\b", 2),                 # → Башкортостан
    (r"\bижевск", 18),               # → Удмуртия
    (r"\bсимферополь", 82),
    (r"\bсевастополь", 92),
    (r"\bкалинингра", 39),
    (r"\bмагас", 6),                 # → Ингушетия
    (r"\bназрань", 6),
    (r"\bдербент", 5),
    (r"\bкаспийск", 5),
    (r"\bпетрозаводск", 10),
    (r"\bсыктывкар", 11),
    (r"\bэлиста", 8),
    (r"\bгорно[- ]?алтайск", 4),
    (r"\bякутск", 14),
    (r"\bекатеринбург", 66),          # → Свердловская
    (r"\bбайконур", 99),               # → Иные территории / Байконур
    (r"\bгергебил", 5),                # → Дагестан (район)
    (r"\bкудутль", 5),
    (r"\bпутятин", 62),                # → Рязанская (село)
    (r"\bпарфеньев", 44),              # → Костромская
    (r"\bпавино", 44),
    (r"\bпоназырев", 44),
    (r"\bбоговарово", 44),
    (r"\bшарья", 44),
    (r"\bколомн", 50),                 # г. Коломна → Московская
    (r"\bподольск", 50),
    (r"\bхимки", 50),
    (r"\bмытищ", 50),
    (r"\bтахтамукай", 1),              # → Адыгея
    (r"\bизобильн", 26),               # → Ставропольский
    (r"\bсветлоград", 26),
    (r"\bнарьян[- ]?мар", 83),         # → НАО
    (r"\bбиробидж", 79),               # → ЕАО
    (r"\bвеликие\s+луки", 60),         # → Псковская
    (r"\bдзержинск", 52),               # → Нижегородская (г. Дзержинск)
    (r"\bелизовск", 41),                # → Камчатский край
    (r"\bпалана\b", 41),                # → Камчатский (пгт Палана)
    (r"\bг\.?\s*сарапул", 18),          # → Удмуртия
    (r"\bоренбурск", 56),               # опечатка "Оренбурская"
    (r"\bбузулук", 56),                 # → Оренбургская
    (r"\bбереговск", 41),
]


def normalize(s: str) -> str:
    """Нормализует строку для поиска паттернов."""
    if not s:
        return ""
    s = s.lower().replace("ё", "е").replace("_x000d_", " ")
    # OCR-мусор: "0Республика" → "0 Республика"
    s = re.sub(r"(\d)([а-я])", r"\1 \2", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_region_number(*address_parts: str) -> int | None:
    """Возвращает номер региона РФ по адресу (или None)."""
    raw = " | ".join(p for p in address_parts if p)
    s = normalize(raw)
    if not s:
        return None
    for pat, num in REGION_PATTERNS:
        if re.search(pat, s):
            return num
    for pat, num in CITY_ALIASES:
        if re.search(pat, s):
            return num
    return None


class Command(BaseCommand):
    help = "Назначить поле region у LegalEntity на основе адресов"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--only-empty",
            action="store_true",
            help="Только юрлица без региона (не перезаписывать уже назначенные)",
        )
        parser.add_argument(
            "--show-unmatched",
            action="store_true",
            help="Выводить адреса, которым не нашли регион",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        only_empty = opts["only_empty"]
        show_unmatched = opts["show_unmatched"]

        regions_by_num = {r.number: r for r in Region.objects.all()}
        self.stdout.write(f"В справочнике регионов: {len(regions_by_num)}")

        qs = LegalEntity.objects.all()
        if only_empty:
            qs = qs.filter(region__isnull=True)

        total = qs.count()
        self.stdout.write(f"Юрлиц к обработке: {total}")

        matched = unmatched = changed = 0
        missing_region = []  # (num, sample_address)
        unmatched_samples = []

        updates: list[LegalEntity] = []
        with transaction.atomic():
            for le in qs.only(
                "id", "region_id",
                "legal_address", "actual_address", "postal_address",
            ).iterator(chunk_size=500):
                num = find_region_number(
                    le.legal_address, le.actual_address, le.postal_address,
                )
                if num is None:
                    unmatched += 1
                    if show_unmatched and len(unmatched_samples) < 30:
                        addr = le.legal_address or le.actual_address or le.postal_address or ""
                        unmatched_samples.append(addr[:140])
                    continue

                region = regions_by_num.get(num)
                if region is None:
                    if len(missing_region) < 10:
                        missing_region.append((num, le.legal_address[:100]))
                    continue

                matched += 1
                if le.region_id != region.id:
                    changed += 1
                    if not dry_run:
                        le.region_id = region.id
                        updates.append(le)

                if not dry_run and len(updates) >= 500:
                    LegalEntity.objects.bulk_update(updates, ["region"])
                    updates.clear()

            if not dry_run and updates:
                LegalEntity.objects.bulk_update(updates, ["region"])

        self.stdout.write(self.style.SUCCESS(
            f"Сопоставлено: {matched}, не сопоставлено: {unmatched}, "
            f"обновлено: {changed}"
        ))
        if missing_region:
            self.stdout.write(self.style.WARNING(
                f"Нет Region для номеров: "
                + ", ".join(f"{n} ({a})" for n, a in missing_region)
            ))
        if unmatched_samples:
            self.stdout.write("Примеры адресов без региона:")
            for s in unmatched_samples:
                self.stdout.write(f"  {s}")
