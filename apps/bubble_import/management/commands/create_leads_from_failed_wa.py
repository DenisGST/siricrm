"""Автосоздать клиентов-лидов для непривязанных WhatsApp-сообщений.

Собираем уникальные номера из BubbleRecord(MessageWSP, status='error',
«Клиент с номером ... не найден»), для каждого создаём лида (как при
онлайн WhatsApp-обращении: status='lead', распределение через
route_new_lead), затем сбрасываем все эти error-записи в pending+approved
— чтобы стандартный apply_approved их догнал.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.bubble_import.appliers import _wa_client_phone
from apps.bubble_import.models import BubbleRecord
from apps.crm.lead_routing import route_new_lead
from apps.crm.models import Client
from apps.crm.phone_utils import add_client_phone, find_client_by_phone


class Command(BaseCommand):
    help = "Создать клиентов-лидов из непривязанных WhatsApp-сообщений"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        qs = BubbleRecord.objects.filter(
            entity="MessageWSP", status="error",
            error__icontains="Клиент с номером",
        )
        total = qs.count()
        self.stdout.write(f"Анализирую {total} ошибочных MessageWSP…")

        # Группируем bubble-id по реальному номеру отправителя.
        groups: dict[str, list[str]] = defaultdict(list)
        for rec in qs.iterator(chunk_size=1000):
            phone = _wa_client_phone(rec.raw or {})
            if phone:
                groups[phone].append(rec.bubble_id)

        self.stdout.write(f"Найдено {len(groups)} уникальных номеров")
        if opts["dry_run"]:
            for p, ids in sorted(groups.items(), key=lambda x: -len(x[1]))[:10]:
                self.stdout.write(f"  +{p}: {len(ids)} сообщений")
            return

        created = 0
        already = 0
        for phone, _ids in groups.items():
            existing = find_client_by_phone(phone)
            if existing is not None:
                already += 1
                continue
            legacy_wa = (
                phone if not Client.objects.filter(whatsapp_phone=phone).exists()
                else None
            )
            c = Client.objects.create(
                first_name="WhatsApp",
                phone="+" + phone,
                whatsapp_phone=legacy_wa,
                status="lead",
                last_message_at=timezone.now(),
            )
            add_client_phone(c, phone, purpose="whatsapp")
            add_client_phone(c, phone, purpose="primary")
            try:
                route_new_lead(
                    c, source_label="WhatsApp (исторический импорт)",
                    event_description=(
                        f"Автосоздан лид по непривязанным сообщениям WhatsApp "
                        f"с номера +{phone}. Историческая переписка будет "
                        f"привязана при следующем apply."
                    ),
                )
            except Exception as e:  # noqa: BLE001
                self.stderr.write(f"route_new_lead failed for +{phone}: {e}")
            created += 1

        # Сбрасываем error-записи в pending+approved, чтобы apply_approved
        # MessageWSP мог их применить (теперь клиент есть в ClientPhone).
        reset = qs.update(
            approved=True, status="pending", error="", imported_at=None,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Создано клиентов-лидов: {created} "
            f"(уже было: {already}). "
            f"Сброшено в pending: {reset}. "
            f"Запусти 'python manage.py apply_bubble MessageWSP' "
            f"чтобы прогнать."
        ))
