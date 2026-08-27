"""Рабочее место оператора колл-центра — доска (канбан) и её колонки.

Два объекта:

* ``CallCenterColumn`` — колонка доски. Название, порядок, цвет и лимит
  задаёт администратор в Панели управления → «Колл-центр»: колонки —
  ДАННЫЕ, а не хардкод (в отличие от главного канбана, где пять колонок
  зашиты в шаблон по ``Client.status``).
* ``CallCenterCard`` — карточка на доске: клиент в конкретной колонке.
  Связка отдельной моделью, а не полем на ``Client``, по двум причинам:
  статус клиента и его положение на доске оператора — разные вещи (главный
  канбан не должен ездить от обзвона), и карточка позже обрастёт своими
  полями (дата перезвона, итог разговора) без правки ``crm.Client``.
"""
import uuid

from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class CallCenterColumn(TimeStampedModel):
    """Колонка доски колл-центра (настраивается администратором)."""

    COLOR_CHOICES = [
        ("neutral", "Серый"),
        ("primary", "Основной"),
        ("info", "Синий"),
        ("success", "Зелёный"),
        ("warning", "Жёлтый"),
        ("error", "Красный"),
    ]
    # Токены daisyUI для inline-стилей: класс badge-<color> собран не для всех
    # вариантов, поэтому цвет подставляем через CSS-переменную темы.
    COLOR_VARS = {
        "neutral": "--n",
        "primary": "--p",
        "info": "--in",
        "success": "--su",
        "warning": "--wa",
        "error": "--er",
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=100, unique=True)
    description = models.CharField(
        "Подсказка", max_length=255, blank=True,
        help_text="Короткое пояснение оператору — показывается в шапке колонки.",
    )
    color = models.CharField("Цвет", max_length=16, choices=COLOR_CHOICES, default="neutral")
    order = models.PositiveIntegerField("Порядок", default=0)
    is_default = models.BooleanField(
        "Колонка по умолчанию", default=False,
        help_text="Сюда попадают карточки, добавленные на доску. "
                  "Такая колонка одна — при включении флаг снимается с прежней.",
    )
    wip_limit = models.PositiveIntegerField(
        "Лимит карточек", default=0,
        help_text="0 — без лимита. При превышении счётчик колонки краснеет "
                  "(мягкое ограничение, перетаскивать не мешает).",
    )
    # ── Автонаполнение ──────────────────────────────────────────────────
    # Колонка «ловит» источник: карточка из него встаёт именно сюда. Флаг —
    # он же выключатель источника: не отмечен ни у одной колонки, значит
    # автодобавление из этого источника не работает (некуда класть).
    # 🛑 Источник — на КОЛОНКЕ, а не в settings: иначе имя колонки уехало бы
    # в код, и переименование в панели ломало бы приём.
    catch_unknown_calls = models.BooleanField(
        "Ловит входящие звонки с неизвестных номеров", default=False,
        help_text="Входящий с номера, которого нет в базе: заводится "
                  "неидентифицированный клиент и его карточка встаёт в эту "
                  "колонку. Колонка-приёмник одна.",
    )
    catch_telegram_leads = models.BooleanField(
        "Ловит лиды из Telegram-канала", default=False,
        help_text="Заявка с лендинга, пришедшая в канал лидов, попадает "
                  "карточкой в эту колонку. Колонка-приёмник одна.",
    )
    is_active = models.BooleanField("Активна", default=True)

    class Meta:
        verbose_name = "Колонка колл-центра"
        verbose_name_plural = "Колонки колл-центра"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    # Флаг-приёмник → ключ источника в apps.callcenter.intake.
    CATCH_FLAGS = {
        "catch_unknown_calls": "call",
        "catch_telegram_leads": "tg_lead",
    }

    def save(self, *args, **kwargs):
        # Колонка по умолчанию одна на всю доску — как is_default у
        # DashboardConfig. То же и с приёмниками: два «ловца» одного
        # источника означали бы, что карточка попадает то туда, то сюда.
        exclusive = ["is_default", *self.CATCH_FLAGS]
        for field in exclusive:
            if getattr(self, field):
                (CallCenterColumn.objects.filter(**{field: True})
                 .exclude(pk=self.pk).update(**{field: False}))
        super().save(*args, **kwargs)

    @property
    def color_var(self) -> str:
        return self.COLOR_VARS.get(self.color, "--n")


class CallCenterCard(TimeStampedModel):
    """Клиент на доске колл-центра (одна карточка на клиента)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.OneToOneField(
        "crm.Client",
        on_delete=models.CASCADE,
        related_name="callcenter_card",
        verbose_name="Клиент",
    )
    column = models.ForeignKey(
        CallCenterColumn,
        on_delete=models.PROTECT,
        related_name="cards",
        verbose_name="Колонка",
    )
    # Момент последнего перемещения — из него растёт «сколько дней висит»
    # в колонке. created_at для этого не годится: он про попадание на доску.
    # ── Владелец карточки ───────────────────────────────────────────────
    # Пусто = общий пул: карточка пришла из автонаполнения и её ещё никто не
    # взял. Назначается ТОЛЬКО вручную кнопкой «Взять в работу» — так видно,
    # кто за что реально взялся, а не кто случайно кликнул первым.
    operator = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="callcenter_cards",
        verbose_name="Оператор",
    )
    taken_at = models.DateTimeField("Взята в работу", null=True, blank=True)

    # ── Следующее действие ──────────────────────────────────────────────
    # Суть + запланированный момент. На этом позже вырастет напоминалка:
    # доставку берём на существующем механизме (notifications.Notification
    # + beat-задача revive_snoozed), второй параллельный не заводим.
    next_action = models.CharField(
        "Следующее действие", max_length=255, blank=True,
        help_text="Что сделать: «Позвонить», «Отправить договор», …",
    )
    next_action_at = models.DateTimeField(
        "Когда", null=True, blank=True, db_index=True,
        help_text="Запланированные дата и время. Индекс нужен будущей "
                  "напоминалке: она выбирает наступившие по времени.",
    )
    next_action_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="callcenter_planned_actions",
        verbose_name="Кто запланировал",
    )
    moved_at = models.DateTimeField("Перемещена", auto_now_add=True)
    SOURCE_MANUAL = "manual"
    SOURCE_CALL = "call"
    SOURCE_TG_LEAD = "tg_lead"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Вручную"),
        (SOURCE_CALL, "Входящий звонок"),
        (SOURCE_TG_LEAD, "Лид из Telegram"),
    ]
    source = models.CharField(
        "Источник", max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MANUAL,
        help_text="Откуда карточка попала на доску — видно оператору значком.",
    )
    moved_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="callcenter_moves",
        verbose_name="Кто переместил",
    )

    class Meta:
        verbose_name = "Карточка колл-центра"
        verbose_name_plural = "Карточки колл-центра"
        ordering = ["-moved_at"]
        indexes = [models.Index(fields=["column", "-moved_at"])]

    def __str__(self):
        return f"{self.client} — {self.column}"

    # ── помощники отображения ───────────────────────────────────────────

    @property
    def has_action(self) -> bool:
        return bool(self.next_action or self.next_action_at)

    @property
    def action_state(self) -> str:
        """Состояние срока: overdue / soon / planned / "" (не запланировано).

        «soon» — ближайший час: столько оператор ещё успевает подготовиться.
        Действие без времени считаем просто запланированным: срок не задан,
        гореть нечему.
        """
        from django.utils import timezone as tz

        if not self.next_action_at:
            return "planned" if self.next_action else ""
        delta = (self.next_action_at - tz.now()).total_seconds()
        if delta < 0:
            return "overdue"
        return "soon" if delta <= 3600 else "planned"

    @property
    def next_action_label(self) -> str:
        """Человеческий момент: «сегодня 13:00», «завтра 09:30», «28.07 13:00»."""
        from django.utils import timezone as tz

        if not self.next_action_at:
            return ""
        local = tz.localtime(self.next_action_at)
        today = tz.localdate()
        days = (local.date() - today).days
        if days == 0:
            return f"сегодня {local:%H:%M}"
        if days == 1:
            return f"завтра {local:%H:%M}"
        if days == -1:
            return f"вчера {local:%H:%M}"
        year = "" if local.year == today.year else f".{local:%Y}"
        return f"{local:%d.%m}{year} {local:%H:%M}"


class BlockedPhone(TimeStampedModel):
    """Чёрный список номеров: спам, роботы, ошибочные звонки.

    Смысл узкий и один: не пускать номер в АВТОНАПОЛНЕНИЕ доски. Звонок с
    такого номера по-прежнему попадёт в журнал звонков и в реестр
    пропущенных — там он факт, который стирать нельзя, — но клиента под него
    не заведут и карточку оператору не покажут.

    🛑 Номер хранится в нормализованном виде (``blacklist_key``), иначе
    «+7 (900) 123-45-67» и «89001234567» жили бы в списке двумя разными
    записями и ловился бы только тот вариант, в котором номер записали.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone = models.CharField(
        "Номер", max_length=32, unique=True,
        help_text="Хранится нормализованным: 79001234567.",
    )
    comment = models.CharField(
        "Причина", max_length=255, blank=True,
        help_text="Зачем заблокирован — «робот-обзвон», «реклама», «ошиблись номером».",
    )
    added_by = models.ForeignKey(
        "core.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="blocked_phones",
        verbose_name="Кто добавил",
    )
    # Сколько раз список уже сработал: видно, что номер действительно долбит,
    # и можно оценить, стоит ли держать запись.
    hits = models.PositiveIntegerField("Срабатываний", default=0)
    last_seen_at = models.DateTimeField("Последний звонок", null=True, blank=True)
    is_active = models.BooleanField("Активна", default=True)

    class Meta:
        verbose_name = "Номер в чёрном списке"
        verbose_name_plural = "Чёрный список номеров"
        ordering = ["-last_seen_at", "-created_at"]

    def __str__(self):
        return self.phone

    @property
    def display_phone(self) -> str:
        from apps.crm.phone_utils import format_phone
        return format_phone(self.phone)


class CallResult(TimeStampedModel):
    """Справочник результатов звонка («Договорились о встрече», «Недозвон»…).

    Правится в Панели управления → «Колл-центр» → «Результаты звонков»:
    формулировки у каждого колл-центра свои, в коде им не место.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=100, unique=True)
    hint = models.CharField(
        "Подсказка", max_length=255, blank=True,
        help_text="Когда выбирать этот результат — показывается оператору.",
    )
    color = models.CharField(
        "Цвет", max_length=16, choices=CallCenterColumn.COLOR_CHOICES, default="neutral")
    order = models.PositiveIntegerField("Порядок", default=0)
    # Подсказать оператору сразу запланировать следующий шаг: у «Недозвона»
    # и «Просил перезвонить» продолжение есть всегда, у «Отказа» — нет.
    suggest_next_action = models.BooleanField(
        "Предлагать запланировать действие", default=False)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Результат звонка"
        verbose_name_plural = "Результаты звонков"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    @property
    def color_var(self) -> str:
        return CallCenterColumn.COLOR_VARS.get(self.color, "--n")


class CallOutcome(TimeStampedModel):
    """Звонок сотрудника и его итог — то, что спрашивает всплывающая модалка.

    🛑 Запись заводится в момент ЗАВЕРШЕНИЯ звонка по событию AMI, когда
    строки ``telephony.Call`` ещё нет: CDR приезжает с АТС пачкой позже.
    Поэтому ключ — нога звонка (``channel_key``), а ссылка на ``Call``
    проставляется потом, когда CDR доедет.
    """
    DIRECTION_IN = "in"
    DIRECTION_OUT = "out"
    DIRECTION_CHOICES = [(DIRECTION_IN, "Входящий"), (DIRECTION_OUT, "Исходящий")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel_key = models.CharField(
        "Ключ ноги звонка", max_length=80, unique=True,
        help_text="Uniqueid ноги + внутренний номер — как у карточки «вам звонили». "
                  "Идемпотентность: повторное событие не плодит вторую модалку.",
    )
    employee = models.ForeignKey(
        "core.Employee", on_delete=models.CASCADE,
        related_name="call_outcomes", verbose_name="Сотрудник",
    )
    client = models.ForeignKey(
        "crm.Client", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="call_outcomes", verbose_name="Клиент",
    )
    call = models.ForeignKey(
        "telephony.Call", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="callcenter_outcomes", verbose_name="Звонок в журнале",
    )
    direction = models.CharField("Направление", max_length=4, choices=DIRECTION_CHOICES)
    phone = models.CharField("Номер собеседника", max_length=32, blank=True)
    answered = models.BooleanField("Разговор состоялся", default=False)
    started_at = models.DateTimeField("Когда звонили", default=timezone.now, db_index=True)

    result = models.ForeignKey(
        CallResult, on_delete=models.PROTECT, null=True, blank=True,
        related_name="outcomes", verbose_name="Результат",
    )
    comment = models.TextField("Комментарий", blank=True)
    filled_at = models.DateTimeField("Заполнен", null=True, blank=True)
    # Отложен оператором («Позже»): модалка больше не всплывает сама, но
    # звонок остаётся в счётчике незаполненных — долг не теряется.
    postponed_at = models.DateTimeField("Отложен", null=True, blank=True)

    class Meta:
        verbose_name = "Результат состоявшегося звонка"
        verbose_name_plural = "Результаты состоявшихся звонков"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["employee", "filled_at"])]

    def __str__(self):
        return f"{self.get_direction_display()} {self.phone} — {self.result or 'без результата'}"

    @property
    def is_filled(self) -> bool:
        return self.filled_at is not None
