"""DevOps panel models.

Используется на двух серверах (dev и prod), но играет разные роли:
- Dev: панель управления → создаёт DevopsAction, опрашивает prod-агент
- Prod: HTTP-агент → принимает запросы, исполняет в DevopsAgentJob

Обе модели живут в БД своего сервера независимо.
"""
import uuid

from django.conf import settings
from django.db import models


# ============================================================================
# DEV-side: окружения и журнал инициированных действий
# ============================================================================
class Environment(models.Model):
    """Окружение, которым можно управлять с этой панели (обычно prod)."""
    name = models.SlugField("Имя", unique=True, help_text="Например: prod")
    base_url = models.URLField("Base URL", help_text="https://siricrm.ru")
    agent_token_env = models.CharField(
        "Имя env-переменной с токеном",
        max_length=100,
        default="DEVOPS_AGENT_TOKEN_PROD",
        help_text="Переменная окружения с Bearer-токеном агента",
    )
    is_active = models.BooleanField("Активно", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Окружение"
        verbose_name_plural = "Окружения"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.base_url})"


class DevopsAction(models.Model):
    """Журнал действий, инициированных через панель."""

    class ActionType(models.TextChoices):
        STATUS = "status", "Статус"
        BACKUP = "backup", "Бэкап БД"
        LIST_BACKUPS = "list_backups", "Список бэкапов"
        S3_STATS = "s3_stats", "S3: статистика"
        DISK_USAGE = "disk_usage", "Диск: разбивка использования"
        VPN_STATUS = "vpn_status", "VPN: проверка туннеля"
        PULL_DB = "pull_db", "Скопировать БД сюда (prod → dev)"
        PUSH_DB = "push_db", "Залить эту БД в цель (dev → prod)"
        DUMPDATA_TABLES = "dumpdata_tables", "Выгрузить выбранные таблицы (dumpdata)"
        LOADDATA_TABLES = "loaddata_tables", "Загрузить выбранные таблицы (loaddata UPSERT)"
        PULL_TABLES = "pull_tables", "Затянуть выбранные таблицы сюда (prod → dev)"
        PUSH_TABLES = "push_tables", "Залить выбранные таблицы в цель (dev → prod)"
        DEPLOY = "deploy", "Деплой"
        GIT_LOG = "git_log", "Версии (git log)"
        ROLLBACK = "rollback", "Откат версии"
        REBUILD = "rebuild", "Rebuild образов"

    class Status(models.TextChoices):
        QUEUED = "queued", "В очереди"
        RUNNING = "running", "Выполняется"
        DONE = "done", "Готово"
        FAILED = "failed", "Ошибка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, related_name="actions")
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    remote_job_id = models.CharField(max_length=64, blank=True, help_text="UUID job на прод-агенте")
    output = models.TextField(blank=True)
    params = models.JSONField(default=dict, blank=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "DevOps action"
        verbose_name_plural = "DevOps actions"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.get_action_type_display()} → {self.environment.name} [{self.status}]"


# ============================================================================
# PROD-side: задачи, выполняемые агентом
# ============================================================================
class DevopsAgentJob(models.Model):
    """Задача в локальной очереди агента (на prod-сервере)."""

    class Status(models.TextChoices):
        QUEUED = "queued", "В очереди"
        RUNNING = "running", "Выполняется"
        DONE = "done", "Готово"
        FAILED = "failed", "Ошибка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action_type = models.CharField(max_length=20)
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    output = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Agent job"
        verbose_name_plural = "Agent jobs"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.action_type} [{self.status}]"
