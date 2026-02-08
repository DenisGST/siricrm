from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid
from apps.files.models import StoredFile
from apps.core.models import Employee

class TimeStampedModel(models.Model):
    """Base model with created_at and updated_at fields"""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

"""
class Department(TimeStampedModel):
    # Department/Team model
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, verbose_name='Название отдела')
    description = models.TextField(blank=True, verbose_name='Описание')
    manager = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_departments',
        verbose_name='Менеджер отдела'
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'Отдел'
        verbose_name_plural = 'Отделы'
        ordering = ['name']
"""
"""
class Operator(TimeStampedModel):
    # Operator/Agent model
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='operator')
    telegram_id = models.BigIntegerField(unique=True, verbose_name='Telegram ID')
    telegram_username = models.CharField(max_length=255, blank=True)
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        related_name='operators',
        verbose_name='Отдел'
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    is_online = models.BooleanField(default=False, verbose_name='Онлайн')
    joined_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата присоединения')
    last_seen = models.DateTimeField(default=timezone.now, verbose_name='Последний визит')
    clients_count = models.IntegerField(default=0, verbose_name='Количество клиентов')

    def __str__(self):
        return f"{self.user.get_full_name()} (@{self.telegram_username})"

    class Meta:
        verbose_name = 'Оператор'
        verbose_name_plural = 'Операторы'
        ordering = ['-last_seen']
"""

class Client(TimeStampedModel):
    """Customer/Client model"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    telegram_id = models.BigIntegerField(unique=True, verbose_name='Telegram ID')
    first_name = models.CharField(max_length=255, verbose_name='Имя')
    last_name = models.CharField(max_length=255, blank=True, verbose_name='Фамилия')
    patronymic = models.CharField(max_length=255, blank=True, verbose_name='Отчество')
    username = models.CharField(max_length=255, blank=True, verbose_name='Username')
    phone = models.CharField(max_length=20, blank=True, verbose_name='Телефон')
    email = models.EmailField(blank=True, verbose_name='Email')
    notes = models.TextField(blank=True, verbose_name='Заметки')
    last_message_at = models.DateTimeField(null=True, blank=True, verbose_name='Последнее сообщение')
    contacts_confirmed = models.BooleanField(default=False, verbose_name='Контакты подтверждены')
    notes = models.TextField(blank=True, verbose_name='Заметки')
    last_message_at = models.DateTimeField(null=True, blank=True, verbose_name='Последнее сообщение')
    employees = models.ManyToManyField(
        Employee,
        related_name="clients",
        blank=True,
        help_text="Сотрудники, работающие с клиентом",
    )
    
    STATUS_CHOICES = [
        ('lead', 'Лид'),
        ('active', 'Активный'),
        ('inactive', 'Неактивный'),
        ('closed', 'Закрыт'),
    ]
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='lead',
        verbose_name='Статус'
    )
    
    

    def __str__(self):
        return f"{self.first_name} {self.last_name} (@{self.username})"

    class Meta:
        verbose_name = 'Клиент'
        verbose_name_plural = 'Клиенты'
        ordering = ['-last_message_at']
        indexes = [
            models.Index(fields=['telegram_id']),
            models.Index(fields=['status']),
        ]


class Message(TimeStampedModel):
    """Message model for conversations"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Sender can be an employee or system
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_messages',
        verbose_name='Сотрудник'
    )
    
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name='messages',
        verbose_name='Клиент'
    )
    
    MESSAGE_TYPE_CHOICES = [
        ('text', 'Текст'),
        ('image', 'Изображение'),
        ('document', 'Документ'),
        ('system', 'Системное'),
        ('audio', 'Аудио'),

    ]
    message_type = models.CharField(
        max_length=20,
        choices=MESSAGE_TYPE_CHOICES,
        default='text',
        verbose_name='Тип сообщения'
    )
    
    content = models.TextField(verbose_name='Содержание')
    
    # Telegram message ID for reference
    telegram_message_id = models.BigIntegerField(null=True, blank=True, verbose_name='ID в Telegram')
    
    # File attachment (S3 path)
    file_url = models.URLField(blank=True, verbose_name='URL файла')
    file_name = models.CharField(max_length=255, blank=True, verbose_name='Имя файла')
    file = models.ForeignKey(
        StoredFile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages",
        verbose_name="Файл",
    )
    
    # Direction
    DIRECTION_CHOICES = [
        ('incoming', 'Входящее'),
        ('outgoing', 'Исходящее'),
    ]
    direction = models.CharField(
        max_length=20,
        choices=DIRECTION_CHOICES,
        default='incoming',
        verbose_name='Направление'
    )
    
    is_read = models.BooleanField(default=False, verbose_name='Прочитано')
    read_at = models.DateTimeField(null=True, blank=True, verbose_name='Время прочтения')

    def __str__(self):
        return f"Message from {self.employee} to {self.client} at {self.created_at}"

    class Meta:
        verbose_name = 'Сообщение'
        verbose_name_plural = 'Сообщения'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['client', 'created_at']),
            models.Index(fields=['employee', 'created_at']),
            models.Index(fields=['is_read']),
        ]

"""
class EmployeeLog(models.Model):
   # Audit log for Employee actions
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
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employee_logs',
        verbose_name='Клиент'
    )
    message = models.ForeignKey(
        Message,
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
"""
class Service(TimeStampedModel):
    """Услуга, привязанная к клиенту"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="services",
        verbose_name="Клиент",
    )

    # Агент (отдельный клиент, который привёл этого клиента)
    agent = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_services",
        verbose_name="Агент",
    )

    # Наименование услуги
    SERVICE_NAME_CHOICES = [
        ("BFL", "БФЛ"),
        ("DTP", "ДТП"),
        ("ZALIV", "Залив"),
        ("ZPP", "ЗПП"),
        ("OTHER", "Прочее"),
    ]
    name = models.CharField(
        max_length=20,
        choices=SERVICE_NAME_CHOICES,
        verbose_name="Наименование услуги",
    )

    # Условия для агента
    AGENT_CIRCS_CHOICES = [
        ("ONCE_3000", "Разово 3000"),
        ("PERCENT", "Проценты"),
        ("INDIVIDUAL", "Индивидуальные условия"),
    ]
    agent_circs = models.CharField(
        max_length=20,
        choices=AGENT_CIRCS_CHOICES,
        verbose_name="Условия для агента",
    )
    agent_circs_notes = models.TextField(
        max_length=1000,
        blank=True,
        verbose_name="Уточнения условий агента",
    )

    # Выплата агентских
    agent_paid = models.IntegerField(
        default=0,
        verbose_name="Выплата агентских",
    )

    # Особые отметки
    special_notes = models.TextField(
        max_length=1000,
        blank=True,
        verbose_name="Особые отметки",
    )

    # Даты / договор
    date_anketa = models.DateField(
        null=True,
        blank=True,
        verbose_name="Дата анкетирования",
    )
    date_dogovor = models.DateField(
        null=True,
        blank=True,
        verbose_name="Дата договора",
    )
    numb_dogovor = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Номер договора",
    )

    # Порядок оплаты
    PAYMENT_PROCEDURE_CHOICES = [
        ("PREPAY", "Предоплата"),
        ("INSTALLMENTS", "Рассрочка"),
        ("POSTPAY", "Постоплата"),
        ("SUCCESS_FEE", "Гонорар успеха"),
        ("SUBSCRIPTION", "Абонентская плата"),
    ]
    payment_procedure = models.CharField(
        max_length=20,
        choices=PAYMENT_PROCEDURE_CHOICES,
        verbose_name="Порядок оплаты",
    )

    # Как оплачивать
    PAYMENT_AS_CHOICES = [
        ("CASHBOX", "Касса"),
        ("ACCOUNT", "Расчетный счет"),
        ("CASH", "Наличные"),
    ]
    payment_as = models.CharField(
        max_length=20,
        choices=PAYMENT_AS_CHOICES,
        verbose_name="Как оплачивать",
    )

    # Статус услуги
    STATUS_SERVICE_CHOICES = [
        ("LEAD", "Лид"),
        ("CONTRACT", "Заключение договора"),
        ("PERFORMANCE", "Исполнение договора"),
        ("DEBTOR", "Должник"),
        ("WARRANTY", "Гарантийное обслуживание"),
        ("ARCHIVE", "Архив"),
    ]
    status_service = models.CharField(
        max_length=20,
        choices=STATUS_SERVICE_CHOICES,
        default="LEAD",
        verbose_name="Статус услуги",
    )

    # Статус в колл-центре
    STATUS_CALLCENTER_CHOICES = [
        ("LEAD_CREATED", "Лид создан"),
        ("NO_ANSWER", "Недозвон"),
        ("BAD_LEAD", "Неликвид"),
        ("LEAD_IN_WORK", "Лид в работе"),
        ("REFUSED_APPOINT", "Отказ на стадии записи на консультацию"),
        ("NON_TARGET", "Нецелевой лид"),
        ("NOT_READY_ASSETS", "Не готов по имуществу"),
        ("WANTED_FREE", "Искал бесплатно"),
        ("APPOINTMENT", "Записан на консультацию"),
        ("TO_CONTRACT", "Передан на заключение договора"),
        ("DEAL_SUPPORT", "Сопровождаю сделку"),
        ("POSTPONED", "Отложено на потом"),
        ("ARCHIVE", "Архив"),
    ]
    status_callcenter = models.CharField(
        max_length=30,
        choices=STATUS_CALLCENTER_CHOICES,
        default="LEAD_CREATED",
        verbose_name="Статус в колл-центре",
    )

    # Статус консультанта
    STATUS_CONSULTANT_CHOICES = [
        ("LEAD", "Лид"),
        ("NO_ANSWER", "Недозвон"),
        ("QUESTIONNAIRE", "Анкетирование"),
        ("NON_TARGET_CLIENT", "Нецелевой клиент"),
        ("APPOINTMENT", "Запись на консультацию"),
        ("THINKING", "Клиент думает"),
        ("TARGET_CHECK", "Определяется целевитость клиента"),
        ("CONTRACT", "Заключение договора"),
        ("LOST", "Пропал"),
        ("CONTRACT_SIGNED", "Договор заключен"),
        ("TO_LAWYERS", "Передан в работу юристов"),
        ("SUPPORT", "Сопровождаю"),
        ("REFUSAL", "Отказ от сотрудничества"),
        ("ARCHIVE", "Архив"),
    ]
    status_consultant = models.CharField(
        max_length=30,
        choices=STATUS_CONSULTANT_CHOICES,
        default="LEAD",
        verbose_name="Статус консультанта",
    )

    # Статус сбора документов
    STATUS_SBOR_CHOICES = [
        ("COLLECTING", "Сбор документов"),
        ("CLAIM_FILED", "Иск подан"),
        ("CLAIM_RETURNED", "Иск возвращен"),
        ("CLAIM_ACCEPTED", "Иск принят"),
        ("TO_FIX", "Доработать"),
        ("HEARING_ASSIGNED", "Назначено СЗ"),
        ("PROCEDURE_STARTED", "Введена процедура"),
        ("TRANSFERRED", "Передано в другой отдел"),
        ("SUPPORT", "Сопровождаю"),
        ("ARCHIVE", "Архив"),
    ]
    status_sbor = models.CharField(
        max_length=30,
        choices=STATUS_SBOR_CHOICES,
        default="COLLECTING",
        verbose_name="Статус сбора документов",
    )
    
    # Статус юротдела (БФЛ)
    STATUS_BFL_CHOICES = [
        ("INPUT", "Ввод"),
        ("RESTRUCT", "Реструктуризация"),
        ("REALIZATION", "Реализация"),
        ("FINISHING", "Завершение"),
        ("FINISHED", "Завершен"),
        ("WARRANTY", "Гарантийное обслуживание"),
        ("ARCHIVE", "Архив"),
    ]
    status_bfl = models.CharField(
        max_length=20,
        choices=STATUS_BFL_CHOICES,
        default="INPUT",
        verbose_name="Статус юротдела (БФЛ)",
    )

    is_active = models.BooleanField(default=True, verbose_name="Активна")

    def __str__(self):
        return f"{self.get_name_display()} ({self.client})"

    class Meta:
        verbose_name = "Услуга"
        verbose_name_plural = "Услуги"
        ordering = ["-created_at"]

