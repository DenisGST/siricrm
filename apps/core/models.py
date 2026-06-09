# apps/core/models.py

from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid

#from apps.crm.models import Client


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        abstract = True

class Department(TimeStampedModel):
    """Department/Team model"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, verbose_name='Название отдела')
    description = models.TextField(blank=True, verbose_name='Описание')
    manager = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_departments',
        verbose_name='Руководитель отдела'
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    sees_all_clients = models.BooleanField(
        "Видит всех клиентов",
        default=False,
        help_text="Сотрудники этого отдела видят всех клиентов компании "
                  "(например, отдел продаж, который сопровождает клиента "
                  "от первого обращения до архива).",
    )
    can_edit_payment_schedule = models.BooleanField(
        "Редактирует график платежей",
        default=False,
        help_text="Сотрудники этого отдела могут составлять/редактировать "
                  "график платежей и начисления (например, коммерческий "
                  "отдел и бухгалтерия). Просмотр графика доступен всем.",
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'Отдел'
        verbose_name_plural = 'Отделы'
        ordering = ['name']

class MenuItem(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=100)
    icon = models.CharField("Иконка", max_length=50, blank=True)
    url = models.CharField("URL", max_length=255)
    section = models.CharField("Секция меню", max_length=100, blank=True)
    order = models.PositiveIntegerField("Порядок", default=0)
    use_htmx = models.BooleanField("Загрузка через HTMX", default=True)
    requires_superuser = models.BooleanField("Только для суперпользователя", default=False)
    requires_elevated = models.BooleanField(
        "Только для администраторов и руководителей",
        default=False,
        help_text="Видим только superuser / admin / head_dep",
    )
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Пункт меню"
        verbose_name_plural = "Пункты меню"
        ordering = ["section", "order"]

    def __str__(self):
        return self.name


class Widget(TimeStampedModel):
    WIDGET_TYPES = [
        ("stats", "Статистика"),
        ("chart", "График"),
        ("table", "Таблица"),
        ("list", "Список"),
        ("custom", "Кастомный"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=100)
    slug = models.SlugField("Идентификатор", unique=True)
    widget_type = models.CharField("Тип", max_length=20, choices=WIDGET_TYPES, default="custom")
    template_name = models.CharField("Шаблон", max_length=255, blank=True)
    description = models.TextField("Описание", blank=True)
    order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Виджет"
        verbose_name_plural = "Виджеты"
        ordering = ["order"]

    def __str__(self):
        return self.name


class DashboardConfig(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=100)
    description = models.TextField("Описание", blank=True)
    menu_items = models.ManyToManyField(MenuItem, blank=True, verbose_name="Пункты меню")
    widgets = models.ManyToManyField(Widget, blank=True, verbose_name="Виджеты")
    is_default = models.BooleanField("По умолчанию", default=False)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Конфигурация дашборда"
        verbose_name_plural = "Конфигурации дашбордов"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_default:
            DashboardConfig.objects.filter(is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class Employee(models.Model):
    ROLE_CHOICES = [
        ("operator", "Оператор"),
        ("manager", "Менеджер"),
        ("consultant", "Консультант"),
        ("assitent_legal", "Помощник юриста"),
        ("lawyer", "Юрист"),
        ("head_dep", "Руководитель отдела"),
        ("arbitration", "Арбитражный управляющий"),
        ("arbitr_assistant", "Помощник АУ"),
        ("agent", "Агент"),
        ("managing_partner", "Управляющий партнер"),
        ("accountant", "Бухгалтер"),
        ("admin", "Администратор"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="employee",
        verbose_name="Сотрудник",
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        related_name='employees',
        verbose_name='Отдел'
    )
    role = models.CharField(
        "Роль",
        max_length=20,
        choices=ROLE_CHOICES,
        default="operator",
    )
    
    dashboard_config = models.ForeignKey(
        DashboardConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employees",
        verbose_name="Конфигурация дашборда",
    )
    has_messenger_access = models.BooleanField("Доступ к мессенджеру", default=True)
    services_allowed = models.ManyToManyField(
        "crm.ServiceName",
        blank=True,
        related_name="allowed_employees",
        verbose_name="Доступные услуги",
    )
    patronymic = models.CharField("Отчество", max_length=255, blank=True)
    phone_mobile = models.CharField("Мобильный телефон", max_length=20, blank=True)
    phone_internal = models.CharField("Внутренний номер", max_length=10, blank=True)
    is_active = models.BooleanField("Активен", default=True)
    is_online = models.BooleanField(default=False, verbose_name='Онлайн')
    is_owner = models.BooleanField(
        "Owner (root)",
        default=False,
        help_text="Видит ВСЁ (включая Django-admin). Только для основателя/админа.",
    )
    accept_telegram_leads = models.BooleanField(
        "Принимать лиды из Telegram", default=False,
        help_text="Заявки с лендингов через @Sirius_system_bot будут "
                  "попадать в «Мой канбан» в колонку «Лиды из Telegram».",
    )
    can_handle_scans = models.BooleanField(
        "Обработка входящих сканов", default=False,
        help_text="Доступ к лотку «Входящие сканы»: видеть присланные со "
                  "сканера документы и привязывать их к клиентам.",
    )
    scanner_name = models.CharField(
        "Имя сканера", max_length=100, blank=True, default="",
        help_text="Метка устройства (device) из scan-agent. Сканы с этой "
                  "меткой по умолчанию показываются этому сотруднику в лотке "
                  "«Входящие сканы». Можно задать один и тот же сканер "
                  "нескольким сотрудникам.",
    )
    bubble_id = models.CharField(
        "Bubble ID", max_length=64, blank=True, null=True, unique=True,
        help_text="ID записи User в исходной CRM на bubble.io",
    )
    joined_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата присоединения')
    dismiss_at = models.DateTimeField(auto_now_add=False,null=True, blank=True, verbose_name='Дата увольнения')

    class Meta:
        verbose_name = "Сотрудник"
        verbose_name_plural = "Сотрудники"

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.department})"



class EmployeeLog(models.Model):
    """Audit log for Employee actions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='logs',
        verbose_name='Сотрудник'
    )
    
    ACTION_CHOICES = [
        ('login', 'Вход'),
        ('logout', 'Выход'),
        ('message_sent', 'Сообщение отправлено'),
        ('message_received', 'Сообщение получено'),
        ('client_add', 'Клиент добавлен'),
        ('client_assigned', 'Клиент назначен'),
        ('client_edit', 'Данные Клиента изменены'),
        ('client_reassigned', 'Клиент переназначен'),
        ('client_status_changed', 'Статус клиента изменен'),
        ('note_added', 'Заметка добавлена'),
        ('client_unassigned', 'Клиент разъединен'),
    ]
    action = models.CharField(
        max_length=50,
        choices=ACTION_CHOICES,
        verbose_name='Действие'
    )
    
    description = models.TextField(verbose_name='Описание')
    
    # Context
    client = models.ForeignKey(
        'crm.Client',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employee_logs',
        verbose_name='Клиент'
    )
    message = models.ForeignKey(
        'crm.Message',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='logs',
        verbose_name='Сообщение'
    )
    
    # Request metadata
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name='IP адрес')
    user_agent = models.TextField(blank=True, verbose_name='User Agent')
    
    timestamp = models.DateTimeField(auto_now_add=True, verbose_name='Время')

    def __str__(self):
        return f"{self.employee} - {self.get_action_display()} at {self.timestamp}"

    class Meta:
        verbose_name = 'Лог сотрудника'
        verbose_name_plural = 'Логи осотрудников'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['employee', 'timestamp']),
            models.Index(fields=['action', 'timestamp']),
            models.Index(fields=['client', 'timestamp']),
        ]
