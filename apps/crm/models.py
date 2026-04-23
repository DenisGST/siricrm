# apps/crm/models.py

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid
from apps.files.models import StoredFile
from apps.core.models import Employee
from django.contrib.postgres.fields import JSONField



class Region(models.Model):
    """Справочник регионов с данными суда для госпошлины"""
    number = models.PositiveIntegerField(
        unique=True,
        verbose_name='Номер региона'
    )
    name = models.CharField(
        max_length=255,
        verbose_name='Наименование региона'
    )
    court_name = models.CharField(
        max_length=500,
        verbose_name='Наименование суда'
    )
    court_address = models.TextField(
        verbose_name='Адрес суда'
    )
    court_payment_details = models.TextField(
        verbose_name='Реквизиты суда для госпошлины',
        help_text='БИК, расчётный счёт, получатель, КБК и прочие реквизиты'
    )

    def __str__(self):
        return f'{self.number} — {self.name}'

    class Meta:
        verbose_name = 'Регион'
        verbose_name_plural = 'Регионы'
        ordering = ['number']

class TimeStampedModel(models.Model):
    """Base model with created_at and updated_at fields"""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True



class Client(TimeStampedModel):
    """Customer/Client model"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    telegram_id = models.BigIntegerField(
        blank=True,
        null=True,
        unique=True,
        verbose_name="Telegram ID",
    )
    max_chat_id = models.CharField(max_length=64, blank=True, null=True)
    first_name = models.CharField(max_length=255, verbose_name='Имя')
    last_name = models.CharField(max_length=255, blank=True, verbose_name='Фамилия')
    patronymic = models.CharField(max_length=255, blank=True, verbose_name='Отчество')
    birth_date = models.DateField(null=True, blank=True, verbose_name='Дата рождения')
    birth_place = models.CharField(max_length=500, blank=True,  null=True, verbose_name='Место рождения')
    # Паспортные данные
    passport_series = models.CharField(max_length=4, blank=True, verbose_name='Серия паспорта')
    passport_number = models.CharField(max_length=6, blank=True, verbose_name='Номер паспорта')
    passport_issued_by = models.CharField(max_length=500, blank=True, verbose_name='Кем выдан')
    passport_issued_date = models.DateField(null=True, blank=True, verbose_name='Дата выдачи')
    # Документы
    inn = models.CharField(max_length=12, blank=True, verbose_name='ИНН')
    snils = models.CharField(max_length=14, blank=True, verbose_name='СНИЛС')
    # идентификация
    username = models.CharField(max_length=255, blank=True, verbose_name='Username')
    phone = models.CharField(max_length=20, blank=True, null=True, verbose_name='Телефон')
    email = models.EmailField(blank=True, verbose_name='Email')
    # дополнительно
    notes = models.TextField(blank=True, verbose_name='Заметки')
    last_message_at = models.DateTimeField(null=True, blank=True, verbose_name='Последнее сообщение')
    contacts_confirmed = models.BooleanField(default=False, verbose_name='Контакты подтверждены')
    employees = models.ManyToManyField(
        Employee,
        through="ClientEmployee",
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


class ClientEmployee(models.Model):
    MESSENGER_STATUS_CHOICES = [
        ("open", "Диалог открыт"),
        ("waiting", "Ожидаю ответа"),
        ("closed", "Диалог закрыт"),
    ]

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="client_employees",
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="client_employees",
    )
    messenger_status = models.CharField(
        "Статус мессенджера", max_length=10,
        choices=MESSENGER_STATUS_CHOICES, default="closed",
    )
    status_changed_at = models.DateTimeField(
        "Время изменения статуса", null=True, blank=True,
    )

    class Meta:
        unique_together = ("client", "employee")
        verbose_name = "Связь клиент-сотрудник"
        verbose_name_plural = "Связи клиент-сотрудник"

    def __str__(self):
        return f"{self.client} — {self.employee} ({self.get_messenger_status_display()})"


class Address(TimeStampedModel):
    """Адрес клиента (структура полей — DaData)"""
    ADDRESS_TYPES = [
        ("default", "По умолчанию"),
        ("registration", "Адрес регистрации"),
        ("actual", "Адрес фактического проживания"),
        ("postal", "Почтовый адрес"),
        ("other", "Иное"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE,
        related_name="addresses", verbose_name="Клиент",
    )
    address_type = models.CharField(
        "Тип адреса", max_length=20,
        choices=ADDRESS_TYPES, default="default",
    )
    comment = models.CharField("Комментарий", max_length=500, blank=True)

    result = models.TextField("Полный адрес (стандартизированный)", blank=True)
    source = models.TextField("Исходный ввод", blank=True)

    postal_code = models.CharField("Индекс", max_length=6, blank=True)
    country = models.CharField("Страна", max_length=120, blank=True, default="Россия")
    country_iso_code = models.CharField("ISO-код страны", max_length=2, blank=True)
    federal_district = models.CharField("Федеральный округ", max_length=255, blank=True)

    region_fias_id = models.CharField("ФИАС ID региона", max_length=36, blank=True)
    region_kladr_id = models.CharField("КЛАДР ID региона", max_length=19, blank=True)
    region_with_type = models.CharField("Регион с типом", max_length=255, blank=True)
    region_type_full = models.CharField("Тип региона", max_length=50, blank=True)
    region = models.CharField("Регион", max_length=255, blank=True)

    area_fias_id = models.CharField("ФИАС ID района", max_length=36, blank=True)
    area_with_type = models.CharField("Район с типом", max_length=255, blank=True)
    area_type_full = models.CharField("Тип района", max_length=50, blank=True)
    area = models.CharField("Район", max_length=255, blank=True)

    city_fias_id = models.CharField("ФИАС ID города", max_length=36, blank=True)
    city_kladr_id = models.CharField("КЛАДР ID города", max_length=19, blank=True)
    city_with_type = models.CharField("Город с типом", max_length=255, blank=True)
    city_type_full = models.CharField("Тип города", max_length=50, blank=True)
    city = models.CharField("Город", max_length=255, blank=True)

    city_district_with_type = models.CharField("Район города с типом", max_length=255, blank=True)

    settlement_fias_id = models.CharField("ФИАС ID н.п.", max_length=36, blank=True)
    settlement_with_type = models.CharField("Нас. пункт с типом", max_length=255, blank=True)
    settlement_type_full = models.CharField("Тип нас. пункта", max_length=50, blank=True)
    settlement = models.CharField("Населённый пункт", max_length=255, blank=True)

    street_fias_id = models.CharField("ФИАС ID улицы", max_length=36, blank=True)
    street_with_type = models.CharField("Улица с типом", max_length=255, blank=True)
    street_type_full = models.CharField("Тип улицы", max_length=50, blank=True)
    street = models.CharField("Улица", max_length=255, blank=True)

    house_fias_id = models.CharField("ФИАС ID дома", max_length=36, blank=True)
    house_type_full = models.CharField("Тип дома", max_length=50, blank=True)
    house = models.CharField("Дом", max_length=50, blank=True)
    block_type_full = models.CharField("Тип корпуса/строения", max_length=50, blank=True)
    block = models.CharField("Корпус/строение", max_length=50, blank=True)
    entrance = models.CharField("Подъезд", max_length=10, blank=True)
    floor = models.CharField("Этаж", max_length=10, blank=True)
    flat_type_full = models.CharField("Тип помещения", max_length=50, blank=True)
    flat = models.CharField("Квартира/офис", max_length=50, blank=True)

    fias_id = models.CharField("ФИАС ID", max_length=36, blank=True)
    fias_level = models.CharField("Уровень детализации ФИАС", max_length=2, blank=True)
    kladr_id = models.CharField("КЛАДР ID", max_length=19, blank=True)

    geo_lat = models.CharField("Широта", max_length=15, blank=True)
    geo_lon = models.CharField("Долгота", max_length=15, blank=True)

    qc_geo = models.CharField("Код точности координат", max_length=1, blank=True)
    qc_complete = models.CharField("Код полноты", max_length=1, blank=True)
    qc_house = models.CharField("Код проверки дома", max_length=1, blank=True)
    qc = models.CharField("Код качества", max_length=1, blank=True)

    okato = models.CharField("ОКАТО", max_length=11, blank=True)
    oktmo = models.CharField("ОКТМО", max_length=11, blank=True)
    tax_office = models.CharField("Код ИФНС", max_length=4, blank=True)
    timezone = models.CharField("Часовой пояс", max_length=50, blank=True)

    class Meta:
        verbose_name = "Адрес"
        verbose_name_plural = "Адреса"
        ordering = ["address_type"]

    def __str__(self):
        return f"{self.get_address_type_display()}: {self.result or self.source}"


class LegalEntityKind(TimeStampedModel):
    """Справочник типов юридических лиц (Банк, МФО, СРО, КО, ФНС, ФССП и т. п.)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True, verbose_name="Наименование типа")
    short_name = models.CharField(max_length=50, verbose_name="Сокращённое наименование")

    def __str__(self):
        return self.short_name or self.name

    class Meta:
        verbose_name = "Тип юридического лица"
        verbose_name_plural = "Типы юридических лиц"
        ordering = ["name"]


class LegalEntity(TimeStampedModel):
    """Юридическое лицо"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    ENTITY_TYPE_CHOICES = [
        ("ooo", "ООО"),
        ("ip", "ИП"),
        ("ao", "АО"),
        ("pao", "ПАО"),
        ("other", "Иное"),
    ]
    entity_type = models.CharField(
        max_length=10, choices=ENTITY_TYPE_CHOICES,
        default="ooo", verbose_name="Форма",
    )
    name = models.CharField(max_length=500, verbose_name="Наименование")
    short_name = models.CharField(max_length=255, blank=True, verbose_name="Краткое наименование")
    brand = models.CharField(max_length=255, blank=True, verbose_name="Бренд")

    inn = models.CharField(max_length=12, blank=True, verbose_name="ИНН")
    kpp = models.CharField(max_length=9, blank=True, verbose_name="КПП")
    ogrn = models.CharField(max_length=15, blank=True, verbose_name="ОГРН")
    okpo = models.CharField(max_length=14, blank=True, verbose_name="ОКПО")
    okved = models.CharField(max_length=10, blank=True, verbose_name="ОКВЭД")

    legal_address = models.TextField(blank=True, verbose_name="Юридический адрес")
    actual_address = models.TextField(blank=True, verbose_name="Фактический адрес")
    postal_address = models.TextField(blank=True, verbose_name="Почтовый адрес")

    director_name = models.CharField(max_length=255, blank=True, verbose_name="Руководитель")
    director_title = models.CharField(max_length=255, blank=True, verbose_name="Должность руководителя")

    phone = models.CharField(max_length=20, blank=True, verbose_name="Телефон")
    email = models.EmailField(blank=True, verbose_name="Email")
    website = models.URLField(blank=True, verbose_name="Сайт")

    bank_name = models.CharField(max_length=255, blank=True, verbose_name="Банк")
    bik = models.CharField(max_length=9, blank=True, verbose_name="БИК")
    correspondent_account = models.CharField(max_length=20, blank=True, verbose_name="Корр. счёт")
    settlement_account = models.CharField(max_length=20, blank=True, verbose_name="Расчётный счёт")

    notes = models.TextField(blank=True, verbose_name="Заметки")
    is_active = models.BooleanField(default=True, verbose_name="Активна")

    STATUS_CHOICES = [
        ("active", "Действующая"),
        ("liquidation", "В ликвидации"),
        ("bankruptcy", "Банкротство"),
        ("liquidated", "Ликвидирована"),
    ]
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES,
        default="active", verbose_name="Статус",
    )

    kind = models.ForeignKey(
        "LegalEntityKind",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="legal_entities",
        verbose_name="Тип юридического лица",
    )

    def __str__(self):
        return self.short_name or self.name

    class Meta:
        verbose_name = "Юридическое лицо"
        verbose_name_plural = "Юридические лица"
        ordering = ["name"]


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
        ('video', 'Видео'),
        ('voice', 'Голосовое'),
    ]
    message_type = models.CharField(
        max_length=20,
        choices=MESSAGE_TYPE_CHOICES,
        default='text',
        verbose_name='Тип сообщения'
    )
    
    channel = models.CharField(
        max_length=16,
        choices=[("telegram", "Telegram"), ("max", "MAX")],
        default="telegram",
    )
    
    max_message_id = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        help_text="ID сообщения в MAX",
    )

    raw_payload = models.JSONField(
        blank=True,
        null=True,
        verbose_name="Сырой payload канала",
        help_text="Оригинальные данные от Telegram/MAX и т.п.",
    )

    reply_to = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='replies',
        verbose_name='Ответ на сообщение',
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
    telegram_date = models.DateTimeField(null=True, blank=True, db_index=True)
    is_read = models.BooleanField(default=False, verbose_name='Прочитано')
    read_at = models.DateTimeField(null=True, blank=True, verbose_name='Время прочтения')
    is_sent = models.BooleanField(default=False)  # отправлено (достигло получателя)
    sent_at = models.DateTimeField(null=True, blank=True)
    is_delivered = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)

    reactions = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Реакции",
        help_text='Реакции на сообщение, например: {"👍": 3, "❤️": 1}',
    )

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

