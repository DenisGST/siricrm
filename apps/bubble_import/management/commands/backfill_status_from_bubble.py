"""Backfill Service.common_status и Client.status из Bubble ProjectBFL.statusPrj.

Bubble хранит этап услуги в `ProjectBFL.statusPrj` (FK на таблицу StatusPrj).
При исходном импорте поле не маппилось → 5762 из 5764 услуг остались
без common_status. Эта команда:

  1. Скачивает таблицу StatusPrj из Bubble (16 записей: id → nameStatusPrj).
  2. По жёстко прошитому маппингу (см. STATUS_MAP внизу файла) ставит
     Service.common_status для каждой услуги, чей BubbleRecord(ProjectBFL)
     имеет target_id (UUID Service'а).
  3. Для услуг без backing ProjectBFL или с нераспознанным statusPrj →
     common_status = "Лидогенератор".
  4. Пересчитывает Client.status: для каждого клиента берётся наивысший
     приоритет по PRIORITY среди статусов всех его услуг. Клиенты без
     услуг — не трогаем.

Идемпотентна: повторный запуск не ломает данные, только перезаписывает
по тому же правилу.

Использование:
    python manage.py backfill_status_from_bubble
    python manage.py backfill_status_from_bubble --dry-run
"""
import uuid
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.bubble_import.bubble_api import fetch_page, is_configured
from apps.bubble_import.models import BubbleRecord
from apps.crm.models import Client, Service, ServiceCommonStatus


# Bubble nameStatusPrj → (ServiceCommonStatus.name, Client.status code)
STATUS_MAP = {
    "На удаление":           ("Архив",                 "to_delete"),
    "Неразобран":            ("Лидогенератор",         "unknown"),
    "Договор расторгнут":    ("Архив",                 "archive"),
    "Приостановка договора": ("Архив",                 "closed"),
    "Подготовка иска":       ("Подготовка иска",       "active"),
    "Думают":                ("Консультация",          "lead"),
    "Согласование":          ("Консультация",          "lead"),
    "Отказ":                 ("Архив",                 "refused"),
    "Заключение договора":   ("Заключение договора",   "lead"),
    "Завершен":              ("Обслуживание",          "closed"),
    "Завершение":            ("Завершение",            "active"),
    "Реструктуризация":      ("Реструктуризация",      "active"),
    "Сбор документов":       ("Сбор документов",       "active"),
    "Анкетирование":         ("Консультация",          "lead"),
    "Ввод":                  ("Подготовка иска",       "active"),
    "Реализация":            ("Реализация",            "active"),
}
# Чем меньше число — тем выше приоритет в выборе Client.status
PRIORITY = {
    "active":    1,
    "closed":    2,
    "lead":      3,
    "unknown":   4,
    "refused":   5,
    "archive":   6,
    "to_delete": 7,
}
DEFAULT_SCS = "Лидогенератор"   # для услуг без распознанного Bubble-статуса
DEFAULT_CLIENT = "unknown"      # их вклад в приоритет клиента


class Command(BaseCommand):
    help = "Заполнить Service.common_status и Client.status по Bubble ProjectBFL.statusPrj"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Показать что будет обновлено, без записи в БД",
        )

    def handle(self, *args, **opts):
        if not is_configured():
            raise CommandError("BUBBLE_API_TOKEN не настроен")

        dry = opts["dry_run"]

        # 1. Тянем StatusPrj из Bubble
        self.stdout.write("Скачиваем StatusPrj из Bubble…")
        res = fetch_page("StatusPrj", 0, 100)
        id_to_name = {r["_id"]: r.get("nameStatusPrj") for r in res["results"]}
        self.stdout.write(f"  получено: {len(id_to_name)} записей")

        unknown_names = [n for n in id_to_name.values() if n and n not in STATUS_MAP]
        if unknown_names:
            self.stdout.write(self.style.WARNING(
                f"  ⚠ нет маппинга для {set(unknown_names)}"
            ))

        # Подгружаем справочник
        scs_by_name = {s.name: s for s in ServiceCommonStatus.objects.all()}
        needed = {p[0] for p in STATUS_MAP.values()} | {DEFAULT_SCS}
        missing = needed - scs_by_name.keys()
        if missing:
            raise CommandError(
                f"В справочнике ServiceCommonStatus нет: {missing}. "
                "Создайте через /references/common-statuses/ и повторите."
            )

        # 2. Service.id (UUID) → (scs_name, client_status)
        service_to_pair = {}
        n_unrecognized = Counter()
        n_no_statusprj = 0
        for br in (
            BubbleRecord.objects.filter(entity="ProjectBFL")
            .exclude(target_id__isnull=True)
            .exclude(target_id="")
            .iterator(chunk_size=2000)
        ):
            try:
                sid = uuid.UUID(str(br.target_id))
            except (ValueError, AttributeError):
                continue
            sp_id = br.raw.get("statusPrj")
            name = id_to_name.get(sp_id) if sp_id else None
            if name and name in STATUS_MAP:
                service_to_pair[sid] = STATUS_MAP[name]
            else:
                if sp_id and not name:
                    n_unrecognized[sp_id] += 1
                else:
                    n_no_statusprj += 1
                service_to_pair[sid] = (DEFAULT_SCS, DEFAULT_CLIENT)

        if n_unrecognized:
            self.stdout.write(self.style.WARNING(
                f"  нераспознанные statusPrj ID: {dict(n_unrecognized)}"
            ))
        if n_no_statusprj:
            self.stdout.write(f"  ProjectBFL без statusPrj: {n_no_statusprj}")

        # 3. Дополняем услугами без backing → дефолт
        all_sids = set(Service.objects.values_list("id", flat=True))
        no_link = all_sids - service_to_pair.keys()
        for sid in no_link:
            service_to_pair[sid] = (DEFAULT_SCS, DEFAULT_CLIENT)
        self.stdout.write(
            f"Услуг всего {len(all_sids)}; из Bubble {len(all_sids) - len(no_link)}; "
            f"без backing {len(no_link)}"
        )

        # 4. План: группируем услуги по common_status; считаем Client.status
        by_scs = defaultdict(list)
        for sid, (scs_name, _) in service_to_pair.items():
            by_scs[scs_name].append(sid)

        clients_data = defaultdict(set)
        for sid, cid in Service.objects.values_list("id", "client_id").iterator(chunk_size=5000):
            if cid is None:
                continue
            pair = service_to_pair.get(sid)
            if pair:
                clients_data[cid].add(pair[1])

        by_status = defaultdict(list)
        for cid, statuses in clients_data.items():
            best = min(statuses, key=lambda s: PRIORITY[s])
            by_status[best].append(cid)

        # 5. Печать плана
        self.stdout.write("\n=== Service.common_status (план) ===")
        for scs_name in sorted(by_scs, key=lambda n: -len(by_scs[n])):
            self.stdout.write(f"  {scs_name:40s} {len(by_scs[scs_name]):5d}")

        self.stdout.write("\n=== Client.status (план, по приоритету услуг) ===")
        for status in sorted(by_status, key=lambda s: PRIORITY[s]):
            self.stdout.write(f"  {status:12s} {len(by_status[status]):5d}")

        if dry:
            self.stdout.write(self.style.SUCCESS("\n[dry-run] изменения НЕ записаны"))
            return

        # 6. Apply в транзакции
        with transaction.atomic():
            self.stdout.write("\nПрименяем Service.common_status…")
            # Сортируем: сначала "Лидогенератор" (содержит дефолтные), потом
            # остальные — чтобы перезаписи шли в правильном порядке (это
            # важно если в by_scs есть дубли между UUID-ключами; здесь их
            # нет, но порядок всё равно детерминированный).
            for scs_name in sorted(by_scs, key=lambda n: (n != DEFAULT_SCS, n)):
                ids = by_scs[scs_name]
                Service.objects.filter(pk__in=ids).update(
                    common_status=scs_by_name[scs_name]
                )

            self.stdout.write("Применяем Client.status…")
            for status, ids in by_status.items():
                Client.objects.filter(pk__in=ids).update(status=status)

        self.stdout.write(self.style.SUCCESS("\nГотово."))
