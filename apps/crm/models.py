# apps/crm/models.py

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid
from apps.files.models import StoredFile
from apps.core.models import Employee
from django.contrib.postgres.fields import JSONField

from apps.crm.managers import ClientQuerySet, ServiceQuerySet



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
    court_address = models.ForeignKey(
        "Address",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="courts",
        verbose_name='Адрес суда',
    )
    court_payment_details = models.TextField(
        verbose_name='Реквизиты суда для госпошлины',
        help_text='БИК, расчётный счёт, получатель, КБК и прочие реквизиты'
    )
    court_deposit_details = models.TextField(
        blank=True, default='',
        verbose_name='Реквизиты суда для оплаты депозита',
        help_text='Реквизиты депозитного счёта суда (для внесения денежных средств на депозит)',
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
    whatsapp_phone = models.CharField(
        max_length=20, blank=True, null=True, unique=True,
        verbose_name="WhatsApp phone (E.164 без +)",
        help_text="Например, 79991234567",
    )
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
        ('unknown', 'Неизвестный'),
        ('lead', 'Лид'),
        ('active', 'Активный'),
        ('closed', 'Закрыт'),
        ('archive', 'Архивный'),
        ('refused', 'Отказники'),
        ('to_delete', 'На удаление'),
    ]
    # Статусы, не отображаемые в канбане по клиентам.
    KANBAN_HIDDEN_STATUSES = ('refused', 'to_delete')
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='lead',
        verbose_name='Статус'
    )
    is_identified = models.BooleanField(
        default=False,
        verbose_name='Идентифицирован',
        help_text='ФИО клиента подтверждено сотрудником через модалку «Идентификация»',
    )

    # Доп. атрибуты (часть приходит из импорта Bubble.io)
    GENDER_CHOICES = [
        ('male', 'Мужчина'),
        ('female', 'Женщина'),
    ]
    gender = models.CharField(
        'Пол', max_length=10, choices=GENDER_CHOICES, blank=True, default='',
    )
    is_married = models.BooleanField('В браке', default=False)
    spouse = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='spouse_of', verbose_name='Супруг(а)',
    )
    referral_source = models.CharField(
        'Источник привлечения', max_length=255, blank=True, default='',
        help_text='Откуда клиент узнал о компании',
    )

    # Импорт из Bubble.io — bubble _id для дедупликации при повторном импорте.
    bubble_id = models.CharField(
        'Bubble ID', max_length=64, blank=True, null=True, unique=True,
        help_text='Идентификатор записи в исходной CRM на bubble.io',
    )

    objects = ClientQuerySet.as_manager()

    @property
    def is_from_bubble(self) -> bool:
        return bool(self.bubble_id)

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


class ClientNameHistory(models.Model):
    """Предыдущие ФИО клиента (смена фамилии/имени/отчества).

    Заполняется при импорте из Bubble (поля fNameOld/lNameOld/mNameOld/lastFIO).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="name_history",
        verbose_name="Клиент",
    )
    last_name = models.CharField("Прежняя фамилия", max_length=255, blank=True, default="")
    first_name = models.CharField("Прежнее имя", max_length=255, blank=True, default="")
    patronymic = models.CharField("Прежнее отчество", max_length=255, blank=True, default="")
    note = models.CharField("Комментарий", max_length=500, blank=True, default="")
    created_at = models.DateTimeField("Добавлено", auto_now_add=True)

    class Meta:
        verbose_name = "Прежнее ФИО клиента"
        verbose_name_plural = "История ФИО клиентов"
        ordering = ["client", "-created_at"]

    def __str__(self):
        return f"{self.client}: {self.last_name} {self.first_name} {self.patronymic}".strip()


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
        null=True, blank=True,
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

    region = models.ForeignKey(
        "Region",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="legal_entities",
        verbose_name="Регион (субъект РФ)",
    )

    def __str__(self):
        return self.short_name or self.name

    class Meta:
        verbose_name = "Юридическое лицо"
        verbose_name_plural = "Юридические лица"
        ordering = ["name"]


class ClientEvent(models.Model):
    """Лог событий по клиенту.

    Все значимые действия фиксируются здесь. Набор event_type расширяется
    по мере доработки CRM — добавляем новые choices, не меняя схему.
    """
    EVENT_CHOICES = [
        # --- Общие ---
        ("first_contact",    "Первое обращение"),
        ("status_change",    "Смена статуса"),
        ("client_identified","Клиент идентифицирован"),
        ("note",             "Заметка"),
        # --- Договор ---
        ("contract_created", "Заключение договора"),
        ("contract_terminated", "Расторжение договора"),
        # --- Сотрудники ---
        ("employee_assigned",  "Назначен сотрудник"),
        ("employee_removed",   "Сотрудник снят"),
        # --- Производство ---
        ("dept_assigned",    "Передан в работу отдела"),
        ("claim_filed",      "Подан иск в суд"),
        ("hearing_scheduled","Назначено судебное заседание"),
        ("procedure_started","Введена процедура"),
        ("procedure_ended",  "Окончена процедура"),
        # --- Мессенджер ---
        ("dialog_started",   "Начат диалог"),
        ("dialog_ended",     "Окончен диалог"),
        ("file_received",    "Получен файл"),
        ("file_sent",        "Отправлен файл"),
        # --- Корреспонденция ---
        ("letter_outgoing",  "Направлено исходящее письмо"),
        ("letter_incoming",  "Получено входящее письмо"),
        # --- Услуги ---
        ("service_created",  "Услуга добавлена"),
        ("service_deleted",  "Услуга удалена"),
        # --- Консультации ---
        ("consultation_booked",      "Записан на консультацию"),
        ("consultation_result",      "Результат консультации"),
        ("consultation_transferred", "Консультация перенесена"),
        ("consultation_edited",      "Консультация изменена"),
        # --- Анкеты ---
        ("questionnaire_created", "Анкета создана"),
        ("questionnaire_edited",  "Анкета отредактирована"),
        ("questionnaire_deleted", "Анкета удалена"),
        # --- Финансы ---
        ("schedule_created",   "Составлен график платежей"),
        ("schedule_updated",   "Изменён график платежей"),
        ("payment_in_created", "Внесён входящий платёж"),
        ("payment_in_edited",  "Входящий платёж отредактирован"),
        ("payment_in_deleted", "Входящий платёж удалён"),
        ("payment_out_created","Внесён исходящий платёж"),
        ("payment_out_edited", "Исходящий платёж отредактирован"),
        ("payment_out_deleted","Исходящий платёж удалён"),
        ("charge_overdue",     "Начисление просрочено"),
        # --- Импорт ---
        ("bubble_imported",  "Импортирован из Bubble"),
        ("bubble_enriched",  "Данные дополнены из Bubble"),
        ("lead_received",    "Получен лид с лендинга"),
        # --- Система ---
        ("system",           "Системное событие"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "Client",
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name="Клиент",
    )
    event_type = models.CharField(
        "Тип события", max_length=30, choices=EVENT_CHOICES, default="note",
    )
    description = models.TextField("Описание", blank=True)
    old_value = models.CharField("Старое значение", max_length=255, blank=True)
    new_value = models.CharField("Новое значение", max_length=255, blank=True)
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="client_events",
        verbose_name="Сотрудник",
    )
    created_at = models.DateTimeField("Дата и время", auto_now_add=True)

    class Meta:
        verbose_name = "Событие клиента"
        verbose_name_plural = "События клиентов"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["client", "created_at"]),
        ]

    def __str__(self):
        return f"{self.client} — {self.get_event_type_display()} ({self.created_at:%d.%m.%Y %H:%M})"


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
        choices=[("telegram", "Telegram"), ("max", "MAX"), ("whatsapp", "WhatsApp")],
        default="telegram",
    )

    max_message_id = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        help_text="ID сообщения в MAX",
    )

    whatsapp_message_id = models.CharField(
        max_length=128, blank=True, null=True,
        help_text="ID сообщения в WhatsApp (Meta wamid)",
    )

    bubble_id = models.CharField(
        max_length=64, blank=True, null=True, unique=True,
        help_text="ID записи MessageWSP в исходной CRM на bubble.io",
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
class ServiceName(TimeStampedModel):
    """Справочник: наименования услуг."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = models.CharField("Полное наименование", max_length=255)
    short_name = models.CharField("Сокращённое наименование", max_length=50)
    is_active = models.BooleanField("Активна", default=True)
    departments = models.ManyToManyField(
        "core.Department",
        blank=True,
        related_name="service_names",
        verbose_name="Отделы",
    )

    class Meta:
        verbose_name = "Наименование услуги"
        verbose_name_plural = "Наименования услуг"
        ordering = ["short_name"]

    def __str__(self):
        return f"{self.short_name} — {self.full_name}"


class PaymentProcedure(TimeStampedModel):
    """Справочник: порядок оплаты по договору."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = models.CharField("Полное наименование", max_length=255)
    short_name = models.CharField("Сокращённое наименование", max_length=50)
    description = models.TextField("Описание", blank=True)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Порядок оплаты"
        verbose_name_plural = "Порядок оплаты"
        ordering = ["short_name"]

    def __str__(self):
        return self.short_name


class ServiceCommonStatus(TimeStampedModel):
    """Справочник: общий статус услуги (колонки общего канбана)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service_name = models.ForeignKey(
        ServiceName,
        on_delete=models.CASCADE,
        related_name="common_statuses",
        verbose_name="Услуга",
    )
    department = models.ForeignKey(
        "core.Department",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="service_statuses",
        verbose_name="Ответственный отдел",
    )
    name = models.CharField("Наименование статуса", max_length=100)
    order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Общий статус услуги"
        verbose_name_plural = "Общие статусы услуг"
        ordering = ["service_name", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_name", "name"],
                name="unique_common_status_per_service",
            ),
        ]

    def __str__(self):
        return f"{self.service_name.short_name}: {self.name}"


class ServiceEmployeeStatus(TimeStampedModel):
    """Справочник: статусы услуги у конкретного сотрудника (колонки «Моего канбана»).

    Каждый статус привязан к конкретному общему статусу услуги — это означает,
    что личный статус сотрудника соответствует одному из общих статусов.
    Услуга (ServiceName) берётся через common_status.service_name.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="service_statuses",
        verbose_name="Сотрудник",
    )
    common_status = models.ForeignKey(
        ServiceCommonStatus,
        on_delete=models.CASCADE,
        related_name="employee_statuses",
        verbose_name="Общий статус услуги",
    )
    name = models.CharField("Наименование статуса", max_length=100)
    comment = models.TextField("Комментарий", blank=True)
    order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активен", default=True)

    @property
    def service_name(self):
        return self.common_status.service_name

    class Meta:
        verbose_name = "Статус услуги сотрудника"
        verbose_name_plural = "Статусы услуг сотрудников"
        ordering = ["employee", "common_status__service_name", "common_status__order", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "common_status", "name"],
                name="unique_emp_status_per_emp_common_status",
            ),
        ]

    def __str__(self):
        return f"{self.employee} / {self.common_status.service_name.short_name} / {self.common_status.name}: {self.name}"


class ServiceTag(TimeStampedModel):
    """Справочник: теги сотрудника (для навешивания на услугу)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="service_tags",
        verbose_name="Сотрудник",
    )
    name = models.CharField("Наименование тега", max_length=50)
    color = models.CharField(
        "Цвет (tailwind class)", max_length=30, blank=True, default="",
        help_text="Например: badge-warning, badge-error, badge-info",
    )
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Тег"
        verbose_name_plural = "Теги"
        ordering = ["employee", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "name"],
                name="unique_tag_per_employee",
            ),
        ]

    def __str__(self):
        return f"{self.employee} / {self.name}"


class Service(TimeStampedModel):
    """Услуга, оказываемая клиенту."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="services",
        verbose_name="Клиент",
    )
    agent = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="agent_services",
        verbose_name="Агент",
    )
    name = models.ForeignKey(
        ServiceName,
        on_delete=models.PROTECT,
        related_name="services",
        verbose_name="Услуга",
    )
    region = models.ForeignKey(
        "crm.Region",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="services",
        verbose_name="Регион",
    )

    AGENT_CIRCS_CHOICES = [
        ("ONCE", "Разовая выплата"),
        ("PERCENT", "Процент"),
        ("INDIVIDUAL", "Индивидуальные условия"),
    ]
    agent_circs = models.CharField(
        "Условия для агента",
        max_length=20,
        choices=AGENT_CIRCS_CHOICES,
        blank=True, default="",
    )
    agent_once_amount = models.DecimalField(
        "Сумма разовой выплаты агенту",
        max_digits=12, decimal_places=2,
        null=True, blank=True,
    )
    agent_percent = models.DecimalField(
        "Процент выплаты агенту (от денег клиента)",
        max_digits=5, decimal_places=2,
        null=True, blank=True,
    )
    agent_notes = models.TextField("Комментарии по работе с агентом", blank=True)

    date_dogovor = models.DateField("Дата договора", null=True, blank=True)
    contract_seq = models.PositiveIntegerField(
        "Счётчик номера договора",
        null=True, blank=True, unique=True,
        help_text="Начинается с 1000; формируется автоматически при сохранении",
    )
    numb_dogovor = models.CharField(
        "Номер договора", max_length=50, blank=True,
        help_text="Автоматически '1000-БФЛ' или вводится вручную",
    )

    date_start = models.DateField("Дата начала оказания услуг", null=True, blank=True)
    date_end = models.DateField("Дата окончания оказания услуг", null=True, blank=True)
    date_terminated = models.DateField("Дата расторжения договора", null=True, blank=True)
    date_executed = models.DateField("Дата исполнения услуг по договору", null=True, blank=True)

    contract_file = models.ForeignKey(
        StoredFile,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="contract_services",
        verbose_name="Файл договора",
    )
    contract_price = models.DecimalField(
        "Цена договора, ₽",
        max_digits=14, decimal_places=2,
        null=True, blank=True,
    )

    # Параметры генератора графика платежей (модалка «График платежей»).
    legal_services_amount = models.DecimalField(
        "Юруслуги по договору, ₽",
        max_digits=14, decimal_places=2,
        default=72000,
    )
    installment_months = models.PositiveSmallIntegerField(
        "Рассрочка, мес",
        default=6,
    )
    doc_collection = models.DecimalField(
        "Сбор документов, ₽", max_digits=14, decimal_places=2, default=7500,
    )
    postal_costs = models.DecimalField(
        "Почтовые расходы, ₽", max_digits=14, decimal_places=2, default=0,
    )
    state_duty = models.DecimalField(
        "Гос. пошлина, ₽", max_digits=14, decimal_places=2, default=0,
    )
    fu_fee = models.DecimalField(
        "Вознаграждение ФУ, ₽", max_digits=14, decimal_places=2, default=25000,
    )
    procedure_costs = models.DecimalField(
        "Расходы на процедуру, ₽", max_digits=14, decimal_places=2, default=20000,
    )
    additional_costs = models.DecimalField(
        "Доп. расходы, ₽", max_digits=14, decimal_places=2, default=0,
    )

    # Смещения дат (в месяцах, 0–6) — настраиваются в модалке графика.
    schedule_legal_offset = models.PositiveSmallIntegerField(
        "Первый платёж за юруслуги через, мес", default=2,
    )
    schedule_fu_offset = models.PositiveSmallIntegerField(
        "Вознаграждение ФУ через, мес", default=1,
    )
    schedule_procedure_offset = models.PositiveSmallIntegerField(
        "Расходы на процедуру через, мес", default=2,
    )

    # Метаданные графика: дата составления / последнего изменения + автор.
    schedule_date = models.DateField(
        "Дата составления графика платежей", null=True, blank=True,
    )
    schedule_created_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="schedules_created",
        verbose_name="Кто составил график",
    )
    schedule_updated_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="schedules_updated",
        verbose_name="Кто изменил график",
    )

    payment_procedure = models.ForeignKey(
        PaymentProcedure,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="services",
        verbose_name="Порядок оплаты",
    )
    common_status = models.ForeignKey(
        ServiceCommonStatus,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="services",
        verbose_name="Общий статус услуги",
    )

    employees = models.ManyToManyField(
        Employee,
        through="ServiceEmployeeState",
        through_fields=("service", "employee"),
        related_name="assigned_services",
        blank=True,
        verbose_name="Исполнители",
    )
    tags = models.ManyToManyField(
        ServiceTag,
        through="ServiceTagAssignment",
        related_name="services",
        blank=True,
        verbose_name="Теги",
    )

    is_active = models.BooleanField("Активна", default=True)

    bubble_id = models.CharField(
        'Bubble ID', max_length=64, blank=True, null=True, unique=True,
        help_text='Идентификатор ProjectBFL в исходной CRM на bubble.io',
    )

    objects = ServiceQuerySet.as_manager()

    def __str__(self):
        return f"{self.name.short_name} ({self.client})"

    def save(self, *args, **kwargs):
        # Автонумер: contract_seq начинается с 1000, numb_dogovor = '{seq}-{short_name}'
        if self.contract_seq is None and not self.numb_dogovor:
            last = (
                Service.objects.filter(contract_seq__isnull=False)
                .order_by("-contract_seq").values_list("contract_seq", flat=True).first()
            )
            self.contract_seq = (last or 999) + 1
            short = (self.name.short_name or "").upper() if self.name_id else ""
            self.numb_dogovor = f"{self.contract_seq}-{short}" if short else str(self.contract_seq)
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "Услуга"
        verbose_name_plural = "Услуги"
        ordering = ["-created_at"]


class ServiceEmployeeState(models.Model):
    """M2M: статус услуги у конкретного сотрудника."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="employee_states",
        verbose_name="Услуга",
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="service_states",
        verbose_name="Сотрудник",
    )
    status = models.ForeignKey(
        ServiceEmployeeStatus, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="states",
        verbose_name="Статус",
    )
    updated_at = models.DateTimeField("Обновлено", auto_now=True)
    updated_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        verbose_name="Кем обновлено",
    )

    class Meta:
        verbose_name = "Статус исполнителя"
        verbose_name_plural = "Статусы исполнителей"
        constraints = [
            models.UniqueConstraint(
                fields=["service", "employee"],
                name="unique_state_per_service_employee",
            ),
        ]

    def __str__(self):
        return f"{self.service} — {self.employee}: {self.status or '—'}"


class ServiceTagAssignment(models.Model):
    """Назначение тега сотрудника на услугу."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="tag_assignments",
        verbose_name="Услуга",
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="service_tag_assignments",
        verbose_name="Сотрудник",
    )
    tag = models.ForeignKey(
        ServiceTag, on_delete=models.CASCADE, related_name="assignments",
        verbose_name="Тег",
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Назначение тега"
        verbose_name_plural = "Назначения тегов"
        constraints = [
            models.UniqueConstraint(
                fields=["service", "employee", "tag"],
                name="unique_tag_assignment",
            ),
        ]

    def __str__(self):
        return f"{self.service} — {self.employee}: {self.tag.name}"


class ServiceLog(models.Model):
    """Лог событий по услуге."""
    ACTION_CHOICES = [
        ("status_change", "Смена статуса сотрудника"),
        ("common_status_change", "Смена общего статуса"),
        ("tag_added", "Тег добавлен"),
        ("tag_removed", "Тег удалён"),
        ("assigned", "Сотрудник назначен"),
        ("unassigned", "Сотрудник снят"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="logs",
        verbose_name="Услуга",
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="service_logs", verbose_name="Сотрудник",
    )
    action = models.CharField("Действие", max_length=30, choices=ACTION_CHOICES)
    old_status = models.ForeignKey(
        ServiceEmployeeStatus, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+", verbose_name="Старый статус",
    )
    new_status = models.ForeignKey(
        ServiceEmployeeStatus, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+", verbose_name="Новый статус",
    )
    old_common_status = models.ForeignKey(
        ServiceCommonStatus, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+", verbose_name="Старый общий статус",
    )
    new_common_status = models.ForeignKey(
        ServiceCommonStatus, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+", verbose_name="Новый общий статус",
    )
    tag = models.ForeignKey(
        ServiceTag, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+", verbose_name="Тег",
    )
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField("Время", auto_now_add=True)

    class Meta:
        verbose_name = "Лог услуги"
        verbose_name_plural = "Логи услуг"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["service", "created_at"]),
            models.Index(fields=["employee", "created_at"]),
        ]

    def __str__(self):
        return f"{self.service} — {self.get_action_display()} ({self.created_at:%d.%m %H:%M})"


class MessageTemplate(models.Model):
    """Шаблон сообщения для отправки клиенту через мессенджеры.

    Используется для быстрых ответов (Telegram/MAX — свободный текст) и
    Meta-approved-шаблонов WhatsApp Business (WABA).

    WA-only поля заполняются если в ``channels`` есть ``'whatsapp'``.
    Без модерации Meta WA-шаблон отправлять нельзя — статус контролируется
    через ``whatsapp_meta_status`` (см. apps/whatsapp в дальнейшем).
    """

    CHANNEL_CHOICES = [
        ('telegram', 'Telegram'),
        ('max', 'MAX'),
        ('whatsapp', 'WhatsApp'),
    ]

    WA_STATUS_CHOICES = [
        ('draft', 'Черновик'),
        ('pending', 'На модерации'),
        ('approved', 'Одобрен'),
        ('rejected', 'Отклонён'),
    ]

    WA_CATEGORY_CHOICES = [
        ('UTILITY', 'Сервисный (UTILITY)'),
        ('MARKETING', 'Маркетинговый (MARKETING)'),
        ('AUTHENTICATION', 'Аутентификация (AUTHENTICATION)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField('Название', max_length=200, unique=True)
    body = models.TextField(
        'Текст шаблона',
        help_text='Плейсхолдеры: {{ client.first_name }}, {{ client.last_name }}, '
                  '{{ service.numb_dogovor }}, {{ employee.user.first_name }}. Для WA-шаблонов '
                  'переменные обозначаются {{1}}, {{2}} согласно требованиям Meta.',
    )
    channels = models.JSONField(
        'Каналы',
        default=list,
        help_text='Список из telegram / max / whatsapp.',
    )
    is_active = models.BooleanField('Активен', default=True)

    # WhatsApp Business-specific
    whatsapp_meta_id = models.CharField(
        'Meta template ID', max_length=200, blank=True, default='',
    )
    whatsapp_meta_status = models.CharField(
        'Статус модерации Meta', max_length=20,
        choices=WA_STATUS_CHOICES, default='draft', blank=True,
    )
    whatsapp_meta_rejection = models.TextField(
        'Причина отклонения Meta', blank=True, default='',
    )
    whatsapp_category = models.CharField(
        'Категория WA', max_length=20, choices=WA_CATEGORY_CHOICES,
        blank=True, default='',
    )
    whatsapp_language = models.CharField(
        'Язык WA', max_length=10, default='ru', blank=True,
    )
    whatsapp_params_schema = models.JSONField(
        'Описание параметров WA',
        default=list, blank=True,
        help_text='Список объектов вида {"placeholder": "{{1}}", "example": "Иван"}.',
    )

    created_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='message_templates_created',
    )
    updated_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='message_templates_updated',
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)

    class Meta:
        verbose_name = 'Шаблон сообщения'
        verbose_name_plural = 'Шаблоны сообщений'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def is_for_whatsapp(self) -> bool:
        return 'whatsapp' in (self.channels or [])

