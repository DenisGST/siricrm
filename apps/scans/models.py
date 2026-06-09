import uuid

from django.db import models


class IncomingScan(models.Model):
    """Лоток входящих сканов: документ, присланный со сканера/МФУ (или
    загруженный вручную), который ещё не привязан к клиенту.

    Файл лежит в S3 (``stored_file``); секретарь в UI «Входящие сканы»
    выбирает клиента и папку, после чего создаётся ``files.ClientFile`` и
    запись помечается ``status=assigned``.
    """

    STATUS_PENDING = "pending"
    STATUS_ASSIGNED = "assigned"
    STATUS_DISCARDED = "discarded"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Не привязан"),
        (STATUS_ASSIGNED, "Привязан"),
        (STATUS_DISCARDED, "Отклонён"),
    ]

    SOURCE_AGENT = "agent"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [
        (SOURCE_AGENT, "Сканер (агент)"),
        (SOURCE_MANUAL, "Загрузка вручную"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stored_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="incoming_scans",
        verbose_name="Файл в хранилище",
    )
    filename = models.CharField("Имя файла", max_length=255)
    size = models.BigIntegerField("Размер (байт)", default=0)
    content_type = models.CharField("Тип", max_length=100, blank=True)
    source = models.CharField(
        "Источник", max_length=20, choices=SOURCE_CHOICES, default=SOURCE_AGENT,
    )
    source_meta = models.CharField(
        "Метка источника", max_length=255, blank=True,
        help_text="Например, имя устройства/папки сканера.",
    )

    status = models.CharField(
        "Статус", max_length=20, choices=STATUS_CHOICES,
        default=STATUS_PENDING, db_index=True,
    )
    received_at = models.DateTimeField("Получен", auto_now_add=True)

    # Кто и куда привязал
    client = models.ForeignKey(
        "crm.Client", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="incoming_scans",
        verbose_name="Клиент",
    )
    client_file = models.ForeignKey(
        "files.ClientFile", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        verbose_name="Файл клиента",
    )
    handled_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="Обработал",
    )
    handled_at = models.DateTimeField("Обработан", null=True, blank=True)

    class Meta:
        verbose_name = "Входящий скан"
        verbose_name_plural = "Входящие сканы"
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.filename} ({self.get_status_display()})"
