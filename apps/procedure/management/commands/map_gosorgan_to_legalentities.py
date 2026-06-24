"""Сопоставление Bubble Gosorgan (1862 записи) с уже существующими crm.LegalEntity.

Bubble Gosorgan — справочник госорганов (МРЭО, ГИМС, ИФНС, ДМИ, ...). В Siri
их подмножество есть в crm.LegalEntity. Маппинг — по нормализованному `name`:
если в LegalEntity ровно один кандидат, проставляем `LegalEntity.bubble_id =
Gosorgan._id`. Дальше `import_bubble_correspondence_to_requests --reapply`
расставит `Request.recipient` по этому полю.

  python manage.py map_gosorgan_to_legalentities          # boevoy
  python manage.py map_gosorgan_to_legalentities --dry    # отчёт без сохранений
"""
import re
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.bubble_import import bubble_api
from apps.crm.models import LegalEntity


# Удалить кавычки, знаки препинания, схлопнуть пробелы, lower
PUNCT_RE = re.compile(r"[«»\"'`“”„‟‘’.,;:!?()\[\]/\\—–-]+")
SPACE_RE = re.compile(r"\s+")

# Аббревиатуры → полные формы (для расширения вариантов).
# Применяются как ABBR → FULL и FULL → ABBR (обе стороны).
ABBR_EXPANSIONS = [
    ("ифнс россии",  "инспекция фнс россии"),
    ("уфнс россии",  "управление фнс россии"),
    ("ифнс",         "инспекция фнс"),
    ("уфнс",         "управление фнс"),
    ("мрэо",         "мэо"),
    ("огибдд",       "отдел гибдд"),
    ("угибдд",       "управление гибдд"),
    ("осп",          "отдел судебных приставов"),
    ("уфссп",        "управление федеральной службы судебных приставов"),
    ("пфр",          "пенсионный фонд российской федерации"),
    ("сфр",          "социальный фонд россии"),
    ("росп",         "районный отдел судебных приставов"),
    ("загс",         "орган записи актов гражданского состояния"),
    ("дми",          "департамент муниципального имущества"),
    ("обл",          "область"),
]


def _base_normalize(s: str) -> str:
    if not s:
        return ""
    out = s.lower().strip()
    out = PUNCT_RE.sub(" ", out)
    out = SPACE_RE.sub(" ", out).strip()
    return out


def normalize(name: str) -> str:
    """Базовая нормализация (lower, без пунктуации, схлопнутые пробелы)."""
    return _base_normalize(name)


def variants(name: str) -> set:
    """Все нормализованные варианты строки: исходный + замены каждой аббревиатуры
    в обе стороны. Возвращает множество — для пересечения с индексом LegalEntity.
    """
    base = _base_normalize(name)
    if not base:
        return set()
    out = {base}
    for abbr, full in ABBR_EXPANSIONS:
        # abbr → full и full → abbr
        if abbr in base:
            out.add(_base_normalize(base.replace(abbr, full)))
        if full in base:
            out.add(_base_normalize(base.replace(full, abbr)))
    # Доп. чистка номеров: «№ 9» ↔ «№9»
    extra = set()
    for v in out:
        extra.add(re.sub(r"№\s+(\d)", r"№\1", v))
        extra.add(re.sub(r"№(\d)", r"№ \1", v))
    out.update(extra)
    return {v for v in out if v}


class Command(BaseCommand):
    help = "Сопоставление Bubble Gosorgan → crm.LegalEntity (по нормализованному name)."

    def add_arguments(self, parser):
        parser.add_argument("--dry", action="store_true",
                            help="Не сохранять — только отчёт.")
        parser.add_argument("--force", action="store_true",
                            help="Перезаписывать bubble_id у LegalEntity если он уже стоит.")

    def _fetch_all_gosorgan(self):
        """Bubble API: тянем все Gosorgan порциями по 100."""
        out = []
        cursor = 0
        while True:
            page = bubble_api.fetch_page("Gosorgan", cursor=cursor, limit=100)
            out.extend(page["results"])
            cursor += page["count"]
            if page["remaining"] <= 0:
                break
        self.stdout.write(f"  Bubble Gosorgan: получено {len(out)}")
        return out

    def _build_name_index(self):
        """Словарь {variant: [LegalEntity.id, ...]} по всем LegalEntity (вкл.
        раскрытие аббревиатур через variants()).
        """
        idx = defaultdict(set)
        n = 0
        for le in LegalEntity.objects.only("id", "name", "short_name").iterator(chunk_size=2000):
            for raw in (le.name, le.short_name):
                for v in variants(raw):
                    idx[v].add(le.id)
            n += 1
        # set → list для совместимости
        idx2 = {k: list(v) for k, v in idx.items()}
        self.stdout.write(f"  Индекс LegalEntity: {n} записей, {len(idx2)} уникальных вариантов")
        return idx2

    def handle(self, *args, **opts):
        dry = opts["dry"]
        force = opts["force"]

        if not bubble_api.is_configured():
            self.stderr.write("BUBBLE_API_TOKEN не задан")
            return

        self.stdout.write("Шаг 1: индекс LegalEntity по name/short_name")
        idx = self._build_name_index()

        self.stdout.write("Шаг 2: фетч всех Gosorgan из Bubble")
        gosorgans = self._fetch_all_gosorgan()

        stats = Counter()
        ambiguous = []   # (gos_id, gos_name, matched_ids)
        unmatched = []   # (gos_id, gos_name, type)
        to_set = []      # (le_id, gos_id, name)
        to_overwrite = []  # (le_id, gos_id, old_bid, name)

        for g in gosorgans:
            gid = g.get("_id")
            gname = g.get("name") or ""
            gvars = variants(gname)
            if not gvars:
                stats["no_name"] += 1
                continue

            # Объединяем кандидатов по ВСЕМ вариантам нормализации gname.
            cands = []
            for v in gvars:
                cands.extend(idx.get(v, []))
            if not cands:
                stats["unmatched"] += 1
                unmatched.append((gid, gname, g.get("type") or ""))
                continue
            uniq = list(set(cands))
            if len(uniq) > 1:
                stats["ambiguous"] += 1
                ambiguous.append((gid, gname, uniq))
                continue

            le_id = uniq[0]
            # Проверим текущий bubble_id у этого LegalEntity
            try:
                le = LegalEntity.objects.only("id", "bubble_id", "name").get(pk=le_id)
            except LegalEntity.DoesNotExist:
                stats["le_missing"] += 1
                continue

            if le.bubble_id == gid:
                stats["already_set"] += 1
                continue
            if le.bubble_id and not force:
                to_overwrite.append((le_id, gid, le.bubble_id, gname))
                stats["already_set_other"] += 1
                continue

            to_set.append((le_id, gid, gname))

        # Запись
        if not dry and to_set:
            with transaction.atomic():
                for le_id, gid, _ in to_set:
                    LegalEntity.objects.filter(pk=le_id).update(bubble_id=gid)
            stats["set"] += len(to_set)
        else:
            stats["would_set"] += len(to_set)

        if not dry and force and to_overwrite:
            with transaction.atomic():
                for le_id, gid, _, _ in to_overwrite:
                    LegalEntity.objects.filter(pk=le_id).update(bubble_id=gid)
            stats["overwrote"] += len(to_overwrite)

        # Отчёт
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== ИТОГ ==="))
        for k in ("set", "would_set", "overwrote", "already_set", "already_set_other",
                  "ambiguous", "unmatched", "no_name", "le_missing"):
            self.stdout.write(f"  {k}: {stats[k]}")

        if ambiguous:
            self.stdout.write("")
            self.stdout.write(f"Неоднозначные (топ-10 из {len(ambiguous)}):")
            for gid, name, ids in ambiguous[:10]:
                self.stdout.write(f"  {name[:80]} → {len(ids)} кандидатов")

        if unmatched:
            self.stdout.write("")
            self.stdout.write(f"Не сопоставленные (топ-15 из {len(unmatched)}):")
            for gid, name, t in unmatched[:15]:
                self.stdout.write(f"  [{t[:15]:15}] {name[:90]}")
