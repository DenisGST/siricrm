import uuid
from django.db import models


class StoredFile(models.Model):
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bucket       = models.CharField(max_length=255)
    key          = models.CharField(max_length=1024)
    filename     = models.CharField(max_length=255)
    created_at   = models.DateTimeField(auto_now_add=True)
    content_type = models.CharField(max_length=255, blank=True, default='')
    size         = models.BigIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = "Файл"
        verbose_name_plural = "Файлы"

    def __str__(self):
        return self.filename or self.key


class ClientFolder(models.Model):
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client     = models.ForeignKey(
        "crm.Client", on_delete=models.CASCADE,
        related_name="folders", verbose_name="Клиент",
    )
    parent     = models.ForeignKey(
        "self", on_delete=models.CASCADE,
        null=True, blank=True, related_name="children", verbose_name="Родитель",
    )
    name       = models.CharField("Название", max_length=200)
    slug       = models.CharField("Системный ключ", max_length=50, blank=True)
    order      = models.PositiveIntegerField("Порядок", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Папка клиента"
        verbose_name_plural = "Папки клиентов"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class ClientFile(models.Model):
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    folder       = models.ForeignKey(
        ClientFolder, on_delete=models.CASCADE,
        related_name="files", verbose_name="Папка",
    )
    stored_file  = models.ForeignKey(
        StoredFile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="client_files",
    )
    name         = models.CharField("Имя файла", max_length=255)
    size         = models.BigIntegerField("Размер (байт)", default=0)
    content_type = models.CharField("Тип", max_length=100, blank=True)
    uploaded_by  = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="Загрузил",
    )
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Файл клиента"
        verbose_name_plural = "Файлы клиентов"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name
