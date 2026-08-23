"""Связать внутренние номера АТС с сотрудниками CRM и переразнести звонки.

Источник — поле ``clid`` уже перенесённых звонков: Asterisk кладёт туда
``"Дмитриева Анна Анатольевна" <301>``. Отдельно ходить на АТС не нужно.

🛑 Внутренние номера переходят от человека к человеку (502: Попова до февраля
2026 → Кудинов к августу). Поэтому:
  • текущим владельцем номера считается тот, чьё имя стоит в САМОМ СВЕЖЕМ
    звонке с этого номера, а не самое частое за всю историю;
  • звонки привязываются к сотруднику по имени из CallerID КАЖДОГО звонка,
    а не оптом по номеру — иначе человеку достанутся чужие разговоры.

    python manage.py map_extensions            # показать, ничего не менять
    python manage.py map_extensions --apply    # записать
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from apps.core.models import Employee
from apps.telephony.models import Call
from apps.telephony.services import match_employee_by_name, parse_clid

# Номера отделов и служебные — за ними нет живого человека.
GROUP_EXTENSIONS = {"200", "300", "400", "500", "700", "799", "666"}


class Command(BaseCommand):
    help = "Проставить Employee.sip_extension и переразнести звонки по сотрудникам"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="записать изменения")

    def handle(self, *args, **opts):
        apply = opts["apply"]

        # ── 1. текущий владелец номера = имя из самого свежего звонка ──────
        latest = {}
        for clid, ext, started in (Call.objects.exclude(clid="").exclude(extension="")
                                   .values_list("clid", "extension", "started_at")
                                   .order_by("started_at")):
            name, _ = parse_clid(clid)
            if name:
                latest[ext] = name          # последний перезаписывает предыдущих

        if not latest:
            self.stdout.write(self.style.WARNING(
                "В звонках нет CallerID с ФИО — сперва перенесите журнал звонков."))
            return

        self.stdout.write(self.style.MIGRATE_HEADING("Текущие владельцы номеров:"))
        applied = 0
        for ext in sorted(latest):
            name = latest[ext]
            if ext in GROUP_EXTENSIONS:
                self.stdout.write(f"  {ext}  {name:40} — групповой/служебный, пропуск")
                continue
            emp = match_employee_by_name(name)
            if emp is None:
                self.stdout.write(self.style.WARNING(f"  {ext}  {name:40} — сотрудник не найден"))
                continue
            mark = "=" if emp.sip_extension == ext else "→"
            self.stdout.write(f"  {ext}  {name:40} {mark} {emp}")
            if apply and emp.sip_extension != ext:
                # Номер уникален: если он раньше числился за другим — снимаем.
                Employee.objects.filter(sip_extension=ext).exclude(pk=emp.pk).update(sip_extension="")
                emp.sip_extension = ext
                emp.save(update_fields=["sip_extension"])
                applied += 1

        # ── 2. звонки — по имени из CallerID каждого звонка ────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\nПривязка звонков по CallerID:"))
        groups = defaultdict(list)
        for clid in Call.objects.exclude(clid="").values_list("clid", flat=True).distinct():
            name, _ = parse_clid(clid)
            if name:
                groups[name].append(clid)

        linked = cleared = 0
        for name, clids in sorted(groups.items()):
            emp = match_employee_by_name(name)
            qs = Call.objects.filter(clid__in=clids)
            total = qs.count()
            if emp is None:
                self.stdout.write(self.style.WARNING(f"  {name:40} {total:>5} звонков — сотрудник не найден"))
                continue
            wrong = qs.exclude(employee=emp).count()
            self.stdout.write(f"  {name:40} {total:>5} звонков → {emp}"
                              f"{f' (переразнести {wrong})' if wrong else ''}")
            if apply and wrong:
                linked += qs.exclude(employee=emp).update(employee=emp)

        # ── 3. звонки без ФИО в CallerID (входящие: там номер клиента) ─────
        # 🛑 Такие нельзя вешать на текущего владельца номера: он мог получить
        # номер позже. Строим шкалу владения из звонков, где имя ЕСТЬ, и для
        # каждого безымянного берём владельца на его дату.
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nЗвонки без ФИО в CallerID (владелец номера на дату звонка):"))
        timeline = defaultdict(list)          # ext -> [(дата, сотрудник), ...]
        for clid, ext, started in (Call.objects.exclude(clid="").exclude(extension="")
                                   .values_list("clid", "extension", "started_at")
                                   .order_by("started_at")):
            name, _ = parse_clid(clid)
            if not name:
                continue
            emp = match_employee_by_name(name)
            if emp is None:
                continue
            if not timeline[ext] or timeline[ext][-1][1].pk != emp.pk:
                timeline[ext].append((started, emp))

        def owner_at(ext, when):
            marks = timeline.get(ext) or []
            owner = None
            for date, emp in marks:
                if date <= when:
                    owner = emp
                else:
                    break
            return owner or (marks[0][1] if marks else None)

        nameless = Call.objects.exclude(extension="").filter(clid__regex=r'^"" *<')
        moved = defaultdict(int)
        for call in nameless.only("id", "extension", "started_at", "employee"):
            emp = owner_at(call.extension, call.started_at)
            if emp is None or call.employee_id == emp.pk:
                continue
            moved[str(emp)] += 1
            if apply:
                Call.objects.filter(pk=call.pk).update(employee=emp)
        if moved:
            for who, n in sorted(moved.items()):
                self.stdout.write(f"  {who:40} {n:>5} звонков переразнести")
        else:
            self.stdout.write("  расхождений нет")
        linked += sum(moved.values()) if apply else 0

        if apply:
            self.stdout.write(self.style.SUCCESS(
                f"\nНомеров записано: {applied}; звонков переразнесено: {linked}"
                f"{f'; снято ошибочных привязок: {cleared}' if cleared else ''}"))
        else:
            self.stdout.write(self.style.NOTICE("\nПоказ без изменений. Записать — с ключом --apply"))
