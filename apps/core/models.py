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
    is_docs_collection = models.BooleanField(
        "Отдел сбора документов",
        default=False,
        help_text="Отметьте на отделе сбора документов. При передаче услуги "
                  "в этот отдел в услугу проставляется «Дата передачи в отдел "
                  "сбора документов» (используется в карточке процедуры).",
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
    # Аватар лежит в S3 (media-бакет), тут — только ключ. FileField не годится:
    # MEDIA_ROOT не смонтирован volume'ом, файл пропал бы при рестарте контейнера.
    avatar_key = models.CharField("Аватар (ключ в S3)", max_length=255, blank=True)
    # Персональные креды ЕФРСБ (ЛК fedresurs) — только у сотрудников-АУ
    # (role="arbitration"). Пароль в БД ТОЛЬКО в зашифрованном виде (Fernet,
    # см. apps/core/crypto.py); открытый текст наружу не отдаём — работа через
    # set_efrsb_password()/get_efrsb_password().
    efrsb_login = models.CharField("Логин ЕФРСБ", max_length=255, blank=True)
    efrsb_password_enc = models.CharField(
        "Пароль ЕФРСБ (шифр.)", max_length=512, blank=True)
    # Почтовый ящик для подачи заявок на публикацию в «Коммерсантъ» и приёма счетов
    # (apps.kommersant). Заявку подаёт сам АУ со своего ящика, счёт ИД шлёт ответом
    # ему же — поэтому одна учётка на отправку (SMTP) и приём (IMAP). SMTP/IMAP-хосты
    # определяются автоматически по домену адреса (apps.kommersant.mail_accounts),
    # поэтому в профиле сотрудник вводит только e-mail и пароль.
    # 🛑 Пароль — ТОЛЬКО шифртекст (Fernet), через set/get_kommersant_password().
    kommersant_email = models.EmailField("E-mail для «Коммерсанта»", max_length=255, blank=True)
    kommersant_password_enc = models.CharField(
        "Пароль почты «Коммерсанта» (шифр.)", max_length=512, blank=True)
    # Личная настройка на странице профиля: слать этому сотруднику в MAX
    # уведомления о судебных событиях. По умолчанию выключено у всех.
    notify_court_events_max = models.BooleanField(
        "Уведомлять в MAX о судебных событиях", default=False)
    # То же самое, но в Telegram (через бота уведомлений). Каналы независимы:
    # можно включить оба — придёт в оба, ни одного — не придёт никуда.
    notify_court_events_telegram = models.BooleanField(
        "Уведомлять в Telegram о судебных событиях", default=False)
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
    telegram_chat_id = models.BigIntegerField(
        "Telegram chat_id (уведомления)", null=True, blank=True, unique=True,
        help_text="Привязывается через бота уведомлений по одноразовому коду "
                  "из профиля. Нужен для дублирования уведомлений в Telegram.",
    )
    # MAX user_id для персональных уведомлений (о судебных событиях и т.п.).
    # Привязывается так же, как telegram_chat_id: одноразовый код из профиля →
    # сотрудник пишет его MAX-боту → webhook сохраняет сюда его user_id.
    # Строка (как Client.max_chat_id): MAX отдаёт user_id строкой.
    max_chat_id = models.CharField(
        "MAX chat_id (уведомления)", max_length=64, null=True, blank=True, unique=True,
        help_text="Привязывается через MAX-бота по одноразовому коду из профиля. "
                  "Нужен для персональных уведомлений в MAX (напр. о судебных событиях).",
    )
    scanner_name = models.CharField(
        "Имя сканера", max_length=100, blank=True, default="",
        help_text="Метка устройства (device) из scan-agent. Сканы с этой "
                  "меткой по умолчанию показываются этому сотруднику в лотке "
                  "«Входящие сканы». Можно задать один и тот же сканер "
                  "нескольким сотрудникам.",
    )
    can_merge_clients = models.BooleanField(
        "Объединение клиентов", default=False,
        help_text="Доступ к кнопке «Объединить» в карточке клиента — "
                  "самостоятельное слияние карточек-дублей.",
    )
    can_edit_finance = models.BooleanField(
        "Редактирование финансов", default=False,
        help_text="Право создавать и изменять платежи (входящие/исходящие). "
                  "Точечный флаг — обычно даётся бухгалтерии; можно выдать "
                  "руководству (managing_partner) без изменения роли.",
    )
    can_view_all_clients = models.BooleanField(
        "Видит всех клиентов", default=False,
        help_text="Точечный доступ ко всей клиентской базе без смены роли. "
                  "Даётся сотрудникам, чьи роли не входят в MANAGEMENT_ROLES "
                  "(admin/head_dep/managing_partner), но которым нужен общий обзор "
                  "(например, arbitration-АУ, помогающий с общей загрузкой).",
    )
    # --- Телефония (apps.telephony) ---
    sip_extension = models.CharField(
        "Внутренний номер АТС", max_length=8, blank=True, default="", db_index=True,
        help_text="Трёхзначный номер сотрудника на Asterisk (201, 301, 502…). "
                  "По нему звонки привязываются к сотруднику, работает звонок "
                  "по клику и всплывашка при входящем.",
    )
    can_listen_calls = models.BooleanField(
        "Доступ к ЧУЖИМ записям разговоров", default=False,
        help_text="Свои записи (со своего внутреннего номера) слушает каждый "
                  "сотрудник, отдельного права не нужно. Этот флаг открывает "
                  "записи ВСЕХ сотрудников — руководству он и так доступен, "
                  "флаг нужен для остальных (например, контроля качества). "
                  "Каждое прослушивание протоколируется.",
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

    @property
    def avatar_url(self):
        """Pre-signed ссылка на аватар в S3 (или None, если не загружен).

        Ссылка живёт час, поэтому кэшируем её в Redis чуть меньше (50 мин):
        аватар рисуется в сайдбаре на каждой странице, генерировать подпись
        на каждый рендер незачем. Ключ кэша включает avatar_key — при замене
        аватара старая ссылка автоматически перестаёт использоваться.
        """
        if not self.avatar_key:
            return None
        from django.core.cache import cache
        from apps.files.s3_utils import get_presigned_url

        ck = f"emp:avatar_url:{self.pk}:{self.avatar_key}"
        url = cache.get(ck)
        if not url:
            url = get_presigned_url(
                settings.AWS_STORAGE_BUCKET_NAME, self.avatar_key, expiration=3600,
            )
            if url:
                cache.set(ck, url, 3000)
        return url

    @property
    def initials(self):
        """Фолбэк вместо аватара: первая буква имени (как было в сайдбаре)."""
        return (self.user.first_name or self.user.username or "?")[:1].upper()

    @property
    def is_arbitration_manager(self):
        """Сотрудник-АУ — у него на профиле поля кредов ЕФРСБ."""
        return self.role == "arbitration"

    def set_efrsb_password(self, raw: str):
        """Записать пароль ЕФРСБ в БД в зашифрованном виде (пусто → очистить)."""
        from apps.core.crypto import encrypt_secret
        self.efrsb_password_enc = encrypt_secret(raw) if raw else ""

    def get_efrsb_password(self) -> str:
        """Расшифрованный пароль ЕФРСБ (или пустая строка)."""
        from apps.core.crypto import decrypt_secret
        return decrypt_secret(self.efrsb_password_enc)

    @property
    def has_efrsb_password(self) -> bool:
        return bool(self.efrsb_password_enc)

    def set_kommersant_password(self, raw: str):
        """Пароль почты «Коммерсанта» в БД — в зашифрованном виде (пусто → очистить)."""
        from apps.core.crypto import encrypt_secret
        self.kommersant_password_enc = encrypt_secret(raw) if raw else ""

    def get_kommersant_password(self) -> str:
        from apps.core.crypto import decrypt_secret
        return decrypt_secret(self.kommersant_password_enc)

    @property
    def has_kommersant_password(self) -> bool:
        return bool(self.kommersant_password_enc)

    @property
    def kommersant_mail_configured(self) -> bool:
        """Готов ли ящик АУ к подаче заявок (адрес + сохранённый пароль)."""
        return bool(self.kommersant_email and self.kommersant_password_enc)


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
