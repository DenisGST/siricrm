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
    passport_division_code = models.CharField(max_length=7, blank=True, verbose_name='Код подразделения')
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

    @property
    def responsible_employees(self):
        """ВСЕ ответственные клиента (Client.employees), кроме системного бота,
        по алфавиту. Для показа списка на карточке канбана (добавить/убрать).
        Использует prefetch `client_employees` (employee__user) если он есть."""
        ces = sorted(
            self.client_employees.all(),
            key=lambda ce: (getattr(ce.employee.user, "last_name", "") or "",
                            getattr(ce.employee.user, "first_name", "") or ""),
        )
        return [ce.employee for ce in ces
                if getattr(getattr(ce.employee, "user", None), "username", "") != "sirius_bot"]

    @property
    def primary_employee(self):
        """Ответственный для показа на карточке/в списках — ПОСЛЕДНИЙ назначенный
        (макс. ClientEmployee.id), исключая системного бота. Детерминированно:
        у M2M `employees` нет сортировки, поэтому `employees.all.0` отдавал разных
        в канбане и поиске. Использует prefetch `client_employees` если он есть."""
        ces = sorted(self.client_employees.all(), key=lambda ce: ce.id, reverse=True)
        if not ces:
            return None
        non_bot = [ce for ce in ces
                   if getattr(getattr(ce.employee, "user", None), "username", "") != "sirius_bot"]
        return (non_bot or ces)[0].employee

    @property
    def display_phone(self) -> str:
        """Номер для показа в UI: основной кэш `phone`, иначе whatsapp-номер
        (отформатированный). У части клиентов единственный номер — whatsapp,
        и без этого фолбэка карточка показывала пустой слот телефона."""
        if self.phone:
            return self.phone
        if self.whatsapp_phone:
            from apps.crm.phone_utils import format_phone
            return format_phone(self.whatsapp_phone)
        return ""

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


class ClientPhone(models.Model):
    """Все телефоны клиента: основной, WhatsApp, Telegram, MAX, дополнительные.

    Источник правды для поиска клиента по номеру (входящий WhatsApp, лид с
    лендинга, дедуп при импорте). `Client.phone` и `Client.whatsapp_phone`
    оставлены как кэш — пишутся синхронно при изменении этого справочника.

    Номер хранится в E.164 без «+» (например, 79991234567). Шаблоны/UI при
    показе добавляют «+» сами.
    """
    PURPOSE_CHOICES = [
        ("primary", "Основной"),
        ("whatsapp", "WhatsApp"),
        ("telegram", "Telegram"),
        ("max", "MAX"),
        ("additional", "Дополнительный"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="phones",
        verbose_name="Клиент",
    )
    phone = models.CharField(
        "Номер телефона", max_length=20,
        help_text="E.164 без «+», например 79991234567",
    )
    purpose = models.CharField(
        "Назначение", max_length=20,
        choices=PURPOSE_CHOICES, default="additional",
    )
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Телефон клиента"
        verbose_name_plural = "Телефоны клиента"
        ordering = ["purpose", "phone"]
        constraints = [
            # Один номер может принадлежать только одному клиенту в рамках
            # назначения (один WhatsApp-номер — у одного клиента и т. п.).
            # У того же клиента тот же номер можно зафиксировать с разными
            # назначениями (primary + whatsapp одновременно).
            models.UniqueConstraint(
                fields=["phone", "purpose"],
                name="uniq_clientphone_phone_purpose",
            ),
        ]
        indexes = [
            models.Index(fields=["phone"]),
            models.Index(fields=["client", "purpose"]),
        ]

    def __str__(self):
        return f"+{self.phone} · {self.get_purpose_display()}"


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
    bubble_id = models.CharField(
        "Bubble ID", max_length=64, blank=True, null=True, unique=True,
        help_text="ID соответствующей записи в Bubble (для импорта).",
    )
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
    bubble_id = models.CharField(
        "Bubble ID", max_length=64, blank=True, null=True, unique=True,
        help_text="ID соответствующей записи в Bubble (для импорта Organization).",
    )

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



# ─── Унифицированный лог клиента (новая архитектура) ───────────────────────
# EventType / ActionType — редактируемые справочники.
# ClientLogEntry — единая запись (kind=event|action) с FK на нужный справочник.
# См. CLAUDE.md секцию «Лог клиента».

class EventType(TimeStampedModel):
    """Справочник типов событий. Событие = что произошло.

    Источник (source) — кто сгенерировал событие: система, суд, клиент,
    юр.лицо или сотрудник. На сотрудника-актора пишем в ClientLogEntry.employee.
    """
    SOURCE_CHOICES = [
        ("system",       "Система"),
        ("court",        "Суд"),
        ("client",       "Клиент"),
        ("legal_entity", "Юр. лицо"),
        ("employee",     "Сотрудник"),
    ]
    code = models.CharField("Код", max_length=40, unique=True)
    name = models.CharField("Название", max_length=120)
    source = models.CharField(
        "Источник", max_length=20, choices=SOURCE_CHOICES, default="system",
    )
    description = models.TextField("Описание", blank=True)
    is_system = models.BooleanField(
        "Системный", default=False,
        help_text="Системные типы нельзя удалять из UI.",
    )
    is_manual = models.BooleanField(
        "Доступно для ручного добавления", default=False,
        help_text=(
            "Тип можно выбрать в форме ручного добавления записи в логе "
            "клиента. Авто-генерируемые типы (мессенджер, смена статуса, "
            "импорт и т.п.) не помечаются."
        ),
    )
    is_active = models.BooleanField("Активен", default=True)
    order = models.PositiveIntegerField("Порядок", default=0)
    notifies = models.BooleanField(
        "Порождает уведомление", default=False,
        help_text=(
            "При записи события этого типа сотрудникам, работающим с клиентом, "
            "приходит уведомление (на сайте + в Telegram-боте)."
        ),
    )
    notify_hint = models.CharField(
        "Подсказка-что-делать", max_length=255, blank=True,
        help_text="Текст-подсказка в строке уведомления (что с этим делать).",
    )

    class Meta:
        verbose_name = "Тип события"
        verbose_name_plural = "Типы событий"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class ActionType(TimeStampedModel):
    """Справочник типов действий. Действие = что сотрудник сделал.

    spawns_event — если задан, при записи действия автоматически создаётся
    событие указанного типа с parent=созданное действие. Так моделируем
    «действие порождает событие» (например ActionType `service_create`
    порождает EventType `service_created`).
    """
    code = models.CharField("Код", max_length=40, unique=True)
    name = models.CharField("Название", max_length=120)
    description = models.TextField("Описание", blank=True)
    spawns_event = models.ForeignKey(
        EventType,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="spawned_by_actions",
        verbose_name="Порождает событие",
        help_text="При записи действия автоматически создаётся событие этого типа.",
    )
    is_system = models.BooleanField(
        "Системный", default=False,
        help_text="Системные типы нельзя удалять из UI.",
    )
    is_manual = models.BooleanField(
        "Доступно для ручного добавления", default=False,
        help_text=(
            "Тип можно выбрать в форме ручного добавления записи в логе "
            "клиента. Авто-генерируемые типы (отправка файла из чата, "
            "создание услуги, платежи и т.п.) не помечаются."
        ),
    )
    is_active = models.BooleanField("Активен", default=True)
    order = models.PositiveIntegerField("Порядок", default=0)
    notifies = models.BooleanField(
        "Порождает уведомление", default=False,
        help_text=(
            "При записи действия этого типа сотрудникам, работающим с клиентом, "
            "приходит уведомление (на сайте + в Telegram-боте). Для действий, "
            "порождающих событие (spawns_event), ставьте флаг на стороне события."
        ),
    )
    notify_hint = models.CharField(
        "Подсказка-что-делать", max_length=255, blank=True,
        help_text="Текст-подсказка в строке уведомления (что с этим делать).",
    )

    class Meta:
        verbose_name = "Тип действия"
        verbose_name_plural = "Типы действий"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


# Стандартный порядок действий по событию: M2M «событие → действия».
# Определяется после ActionType, поэтому через add_to_class.
EventType.add_to_class(
    "standard_actions",
    models.ManyToManyField(
        ActionType,
        blank=True,
        related_name="standard_for_events",
        verbose_name="Стандартные действия",
        help_text=(
            "Действия, которые сотрудник должен выполнить при наступлении "
            "этого события. Используются как подсказки в UI."
        ),
    ),
)


class ClientLogEntry(models.Model):
    """Единый лог клиента: события и действия в одной таблице.

    Замена устаревшей `ClientEvent` (миграция 0071). Subject события/действия —
    Client, либо «наша компания» (subject_kind='company'), либо Employee
    (subject_kind='employee'). В этой итерации заполняется только Client.

    parent — связь между записями: action, порождённое стандартной процедурой
    по event'у, указывает на event как parent. Spawned-event от action указывает
    на action как parent (см. helper apps/crm/client_log.py).
    """
    KIND_CHOICES = [("event", "Событие"), ("action", "Действие")]
    SUBJECT_KIND_CHOICES = [
        ("client",   "Клиент"),
        ("company",  "Наша компания"),
        ("employee", "Сотрудник"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bubble_id = models.CharField(
        "Bubble ID", max_length=64, blank=True, null=True, unique=True,
    )

    subject_kind = models.CharField(
        "Subject", max_length=10, choices=SUBJECT_KIND_CHOICES, default="client",
    )
    client = models.ForeignKey(
        "Client", on_delete=models.CASCADE, null=True, blank=True,
        related_name="log_entries", verbose_name="Клиент",
    )
    subject_employee = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="log_as_subject", verbose_name="Сотрудник (subject)",
    )

    kind = models.CharField("Тип записи", max_length=10, choices=KIND_CHOICES)
    event_type = models.ForeignKey(
        EventType, on_delete=models.PROTECT, null=True, blank=True,
        related_name="entries", verbose_name="Тип события",
    )
    action_type = models.ForeignKey(
        ActionType, on_delete=models.PROTECT, null=True, blank=True,
        related_name="entries", verbose_name="Тип действия",
    )

    comment = models.TextField("Комментарий", blank=True)
    old_value = models.CharField("Старое значение", max_length=255, blank=True)
    new_value = models.CharField("Новое значение", max_length=255, blank=True)

    employee = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="log_entries", verbose_name="Сотрудник-актор",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="children", verbose_name="Родительская запись",
    )
    stored_file = models.ForeignKey(
        StoredFile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="log_entries", verbose_name="Файл",
        help_text="Привязанный файл (для событий «Получен/Отправлен файл»)",
    )
    created_at = models.DateTimeField("Дата и время", auto_now_add=True)

    class Meta:
        verbose_name = "Запись лога клиента"
        verbose_name_plural = "Лог клиента"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["client", "created_at"]),
            models.Index(fields=["subject_kind", "kind"]),
            models.Index(fields=["kind", "event_type"]),
            models.Index(fields=["kind", "action_type"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(kind="event", event_type__isnull=False, action_type__isnull=True)
                    | models.Q(kind="action", action_type__isnull=False, event_type__isnull=True)
                ),
                name="log_entry_kind_matches_type_fk",
            ),
        ]

    def __str__(self):
        subj = self.client or self.subject_employee or self.get_subject_kind_display()
        type_obj = self.event_type or self.action_type
        return f"{subj} — {type_obj} ({self.created_at:%d.%m.%Y %H:%M})"


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
    is_failed = models.BooleanField(default=False, verbose_name='Ошибка доставки')
    error_text = models.CharField(max_length=500, blank=True, default='', verbose_name='Текст ошибки')

    reactions = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Реакции",
        help_text='Реакции на сообщение, например: {"👍": 3, "❤️": 1}',
    )

    # WhatsApp WABA-шаблон (отправка вне 24-часового окна через sendTemplate).
    # Для обычных free-form сообщений оба поля пустые.
    message_template = models.ForeignKey(
        "MessageTemplate",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages",
        verbose_name="Шаблон",
    )
    template_params = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Параметры шаблона",
        help_text="Значения {{1}}, {{2}}… по порядку, переданные в sendTemplate.",
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
        null=True, blank=True,
        related_name="employee_statuses",
        verbose_name="Общий статус услуги",
        help_text="Пусто — универсальная колонка-инбокс «Не принято» "
                  "(не привязана к стадии/услуге).",
    )
    is_inbox = models.BooleanField(
        "Инбокс «Не принято»", default=False,
        help_text="Универсальная колонка для услуг, переданных сотруднику/"
                  "отделу и ещё не принятых в работу. Одна на сотрудника, "
                  "не привязана к стадии услуги (common_status пуст).",
    )
    name = models.CharField("Наименование статуса", max_length=100)
    comment = models.TextField("Комментарий", blank=True)
    order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активен", default=True)

    @property
    def service_name(self):
        return self.common_status.service_name if self.common_status_id else None

    class Meta:
        verbose_name = "Статус услуги сотрудника"
        verbose_name_plural = "Статусы услуг сотрудников"
        ordering = ["employee", "common_status__service_name", "common_status__order", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "common_status", "name"],
                name="unique_emp_status_per_emp_common_status",
            ),
            # Один инбокс «Не принято» на сотрудника.
            models.UniqueConstraint(
                fields=["employee"],
                condition=models.Q(is_inbox=True),
                name="unique_inbox_status_per_employee",
            ),
        ]

    def __str__(self):
        if self.common_status_id:
            return f"{self.employee} / {self.common_status.service_name.short_name} / {self.common_status.name}: {self.name}"
        return f"{self.employee} / [инбокс]: {self.name}"


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
    # Заполняется бизнес-логикой при передаче услуги в отдел сбора документов
    # (apps/crm/service_transfer.py). Показывается в карточке процедуры (п.3 дат услуги).
    docs_dept_date = models.DateField(
        "Дата передачи в отдел сбора документов", null=True, blank=True,
    )

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
    total_debt = models.DecimalField(
        "Сумма всех долгов, ₽",
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
    whatsapp_template_name = models.CharField(
        'Имя шаблона в Meta', max_length=200, blank=True, default='',
        help_text='Латиница + подчёркивания (например, first_contact_intro). '
                  'Используется в sendTemplate. Если пусто — генерируется из названия.',
    )
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


# ============================================================================
# Кредиторы клиента (для БФЛ)
# ============================================================================

class Kreditor(TimeStampedModel):
    """Кредитор клиента в рамках процедуры банкротства.

    Жизненный цикл:
    1. Заполняется в анкете БФЛ (source='anketa', sum_anketa со слов клиента).
    2. После сбора справок юрист сверяет и корректирует (source='verified',
       sum_verified — по факту).
    3. В ходе процедуры пишутся события смены статуса (KreditorStatusEvent):
       заявление подано → включено в РТК → оплачено частично / полностью.

    Субъект кредитора — ровно одно из трёх (CheckConstraint):
    legal_entity (юрлицо/ИП/банк/МФО/госорган), client_person (физлицо-
    клиент Сириуса) или name_manual (свободная строка для случаев, когда
    справочник ещё не привязан).
    """
    SOURCE_CHOICES = [
        ('anketa', 'Из анкеты клиента'),
        ('verified', 'По полученным справкам'),
        ('manual', 'Добавлен вручную'),
    ]
    STATUS_CHOICES = [
        ('', '—'),
        ('claim_filed', 'Подано заявление о включении в РТК'),
        ('included', 'Включено в РТК'),
        ('paid_partial', 'Оплачено частично'),
        ('paid_full', 'Оплачено полностью'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bubble_id = models.CharField(
        "Bubble ID", max_length=64, blank=True, null=True, unique=True,
    )

    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name='kreditors',
        verbose_name='Услуга БФЛ',
    )

    legal_entity = models.ForeignKey(
        LegalEntity, on_delete=models.PROTECT,
        null=True, blank=True, related_name='kreditor_of',
        verbose_name='Юрлицо/ИП (кредитор)',
    )
    client_person = models.ForeignKey(
        Client, on_delete=models.PROTECT,
        null=True, blank=True, related_name='as_kreditor_for',
        verbose_name='Физлицо (кредитор)',
    )
    name_manual = models.CharField(
        'Имя кредитора (свободный ввод)', max_length=500, blank=True,
        help_text='Заполняется, когда субъект ещё не привязан к справочнику.',
    )

    source = models.CharField(
        'Источник', max_length=12, choices=SOURCE_CHOICES, default='anketa',
    )
    debt_basis = models.TextField('Основание долга', blank=True)
    sum_anketa = models.DecimalField(
        'Сумма со слов клиента (анкета)', max_digits=14, decimal_places=2,
        null=True, blank=True,
    )
    sum_verified = models.DecimalField(
        'Сумма по справке', max_digits=14, decimal_places=2,
        null=True, blank=True,
    )

    current_status = models.CharField(
        'Текущий статус по процедуре', max_length=20,
        choices=STATUS_CHOICES, blank=True, default='',
    )
    current_status_date = models.DateField(
        'Дата текущего статуса', null=True, blank=True,
    )

    secured_by_collateral = models.BooleanField('Обеспечено залогом', default=False)
    collateral_description = models.TextField('Описание залога', blank=True)

    class Meta:
        verbose_name = 'Кредитор'
        verbose_name_plural = 'Кредиторы'
        ordering = ['-sum_anketa', 'created_at']
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(legal_entity__isnull=False)
                    | models.Q(client_person__isnull=False)
                    | ~models.Q(name_manual='')
                ),
                name='kreditor_has_subject',
            ),
        ]
        indexes = [
            models.Index(fields=['service']),
            models.Index(fields=['current_status']),
        ]

    def __str__(self):
        if self.legal_entity:
            subj = str(self.legal_entity)
        elif self.client_person:
            subj = str(self.client_person)
        else:
            subj = self.name_manual or '—'
        return f'{subj} ({self.sum_anketa or 0} ₽)'


class KreditorStatusEvent(models.Model):
    """История смены статусов кредитора в ходе процедуры банкротства.

    Каждое изменение — отдельная запись с датой/суммой/описанием. Текущий
    статус кредитора (Kreditor.current_status) денормализуется через
    post_save сигнал — это последний event по дате.
    """
    STATUS_CHOICES = [
        ('claim_filed', 'Подано заявление о включении в РТК'),
        ('included', 'Включено в РТК'),
        ('paid_partial', 'Оплачено частично'),
        ('paid_full', 'Оплачено полностью'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kreditor = models.ForeignKey(
        Kreditor, on_delete=models.CASCADE, related_name='status_events',
        verbose_name='Кредитор',
    )
    status = models.CharField('Статус', max_length=20, choices=STATUS_CHOICES)
    date = models.DateField('Дата')
    amount = models.DecimalField(
        'Сумма', max_digits=14, decimal_places=2, null=True, blank=True,
    )
    description = models.TextField('Описание', blank=True)
    employee = models.ForeignKey(
        'core.Employee', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='kreditor_events',
        verbose_name='Сотрудник',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Событие смены статуса кредитора'
        verbose_name_plural = 'События смены статусов кредиторов'
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.kreditor_id} → {self.get_status_display()} ({self.date})'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Денормализуем «последний по дате» статус в Kreditor — чтобы списки
        # кредиторов услуги не делали join к истории на каждый рендер.
        latest = (
            type(self).objects
            .filter(kreditor_id=self.kreditor_id)
            .order_by('-date', '-created_at')
            .first()
        )
        if latest and latest.pk == self.pk:
            Kreditor.objects.filter(pk=self.kreditor_id).update(
                current_status=self.status,
                current_status_date=self.date,
            )


# ============================================================================
# Корреспонденция (исходящие/входящие письма по услугам БФЛ)
# ============================================================================

class Correspondence(TimeStampedModel):
    """Лог переписки по услуге: запросы в госорганы (Росреестр, ИФНС, МРЭО,
    ПФР), информационные письма в банки/приставам, ходатайства, исковые,
    договоры. С контролем ответа.

    Импортируется из Bubble Correspondence; новые записи юристы создают
    через UI (UI добавим отдельно — пока работа только через админку).
    """
    DIRECTION_CHOICES = [
        ('outgoing', 'Исходящее'),
        ('incoming', 'Входящее'),
    ]
    DELIVERY_CHOICES = [
        ('', '—'),
        ('post', 'Почта РФ'),
        ('email', 'Электронная почта'),
        ('site', 'Сайт организации'),
        ('telegram', 'Телеграмма'),
        ('courier', 'Нарочно / курьером'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bubble_id = models.CharField(
        "Bubble ID", max_length=64, blank=True, null=True, unique=True,
    )
    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name='correspondences',
        verbose_name='Услуга',
    )
    counterparty = models.ForeignKey(
        LegalEntity, on_delete=models.PROTECT,
        null=True, blank=True, related_name='correspondences',
        verbose_name='Контрагент',
    )

    direction = models.CharField(
        'Направление', max_length=10, choices=DIRECTION_CHOICES, default='outgoing',
    )
    subject_type = models.CharField(
        'Тип письма', max_length=255, blank=True,
        help_text='Например, «Запрос в Росреестр», «Исковое заявление».',
    )

    outgoing_number = models.CharField('Исходящий номер', max_length=100, blank=True)
    sent_at = models.DateField('Дата отправки', null=True, blank=True)
    delivery_method = models.CharField(
        'Способ отправки', max_length=20, choices=DELIVERY_CHOICES,
        blank=True, default='',
    )
    file_link = models.TextField(
        'Ссылка на файл письма', blank=True,
        help_text='URL Google Drive (исторический) или S3.',
    )

    track_response = models.BooleanField('Отслеживать ответ', default=False)
    control_date = models.DateField('Контрольная дата', null=True, blank=True)
    response_received = models.BooleanField('Получен ответ', default=False)
    response_date = models.DateField('Дата ответа', null=True, blank=True)
    response_text = models.TextField('Текст ответа', blank=True)
    response_number = models.CharField('Номер ответа', max_length=100, blank=True)

    comments = models.TextField('Комментарии', blank=True)

    class Meta:
        verbose_name = 'Корреспонденция'
        verbose_name_plural = 'Корреспонденция'
        ordering = ['-sent_at', '-created_at']
        indexes = [
            models.Index(fields=['service', 'sent_at']),
            models.Index(fields=['direction']),
        ]

    def __str__(self):
        return f'{self.get_direction_display()}: {self.subject_type or self.outgoing_number or "—"} ({self.sent_at})'

