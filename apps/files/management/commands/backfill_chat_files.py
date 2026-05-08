"""
Раскладывает существующие файлы из сообщений чата по папкам клиентов.
Отправленные → Чат/Отправленные
Полученные   → Чат/Полученные

Уже разложенные файлы (по stored_file) пропускаются.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Backfill chat files into client folder structure"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Только показать, ничего не создавать")

    def handle(self, *args, **options):
        from apps.crm.models import Message
        from apps.files.models import ClientFile
        from apps.files.folder_utils import get_chat_folder, create_default_folders

        dry_run = options["dry_run"]

        # Уже разложенные stored_file IDs — пропускаем
        existing_sf_ids = set(
            ClientFile.objects.filter(stored_file__isnull=False)
            .values_list("stored_file_id", flat=True)
        )

        messages = (
            Message.objects
            .filter(file__isnull=False)
            .select_related("client", "file")
            .order_by("created_at")
        )

        total = messages.count()
        created = skipped = errors = 0

        self.stdout.write(f"Найдено сообщений с файлами: {total}")

        for msg in messages.iterator(chunk_size=200):
            if msg.file_id in existing_sf_ids:
                skipped += 1
                continue

            direction = "sent" if msg.direction == "outgoing" else "received"

            try:
                with transaction.atomic():
                    # Создаём папки если их ещё нет
                    if not hasattr(msg.client, "_folders_checked"):
                        create_default_folders(msg.client)
                        msg.client._folders_checked = True

                    folder = get_chat_folder(msg.client, direction)
                    name = msg.file.filename or msg.file_name or "file"

                    if not dry_run:
                        ClientFile.objects.create(
                            folder=folder,
                            stored_file=msg.file,
                            name=name,
                            size=msg.file.size or 0,
                            content_type=msg.file.content_type or "",
                        )
                    created += 1
                    existing_sf_ids.add(msg.file_id)

            except Exception as e:
                errors += 1
                self.stderr.write(f"  Ошибка msg={msg.pk}: {e}")

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}Готово: создано={created}, пропущено={skipped}, ошибок={errors}"
        ))
