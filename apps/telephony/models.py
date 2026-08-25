import uuid

from django.db import models


class Call(models.Model):
    """Один звонок, перенесённый с АТС Asterisk.

    Источник — таблица ``cdr`` на сервере АТС; агент (``tools/pbx-agent/``)
    шлёт метаданные и mp3-запись в CRM по HTTPS с Bearer-токеном.
    Ключ дедупликации — ``uniqueid`` из CDR: повторная отправка того же
    звонка обновляет запись, а не плодит дубли.
    """

    DIRECTION_IN = "incoming"
    DIRECTION_OUT = "outgoing"
    DIRECTION_INTERNAL = "internal"
    DIRECTION_CHOICES = [
        (DIRECTION_IN, "Входящий"),
        (DIRECTION_OUT, "Исходящий"),
        (DIRECTION_INTERNAL, "Внутренний"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- идентификаторы из CDR ---
    uniqueid = models.CharField("Uniqueid", max_length=32, unique=True)
    linkedid = models.CharField(
        "Linkedid", max_length=32, blank=True, db_index=True,
        help_text="Склейка ног одного звонка (перевод, параллельный обзвон).",
    )

    # --- когда и кто ---
    started_at = models.DateTimeField("Начало", db_index=True)
    direction = models.CharField(
        "Направление", max_length=10, choices=DIRECTION_CHOICES, db_index=True,
    )
    src = models.CharField("Кто звонил", max_length=80, blank=True)
    dst = models.CharField("Кому звонили", max_length=80, blank=True)
    clid = models.CharField("CallerID", max_length=120, blank=True)

    extension = models.CharField(
        "Внутренний номер", max_length=8, blank=True, db_index=True,
        help_text="Трёхзначный номер сотрудника на АТС.",
    )
    employee = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="calls", verbose_name="Сотрудник",
    )
    counterparty_phone = models.CharField(
        "Номер абонента", max_length=32, blank=True, db_index=True,
        help_text="Внешний номер в нормализованном виде — по нему ищется клиент.",
    )
    client = models.ForeignKey(
        "crm.Client", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="calls", verbose_name="Клиент",
    )

    # --- как прошёл ---
    duration = models.IntegerField("Длительность, с", default=0)
    billsec = models.IntegerField("Разговор, с", default=0)
    disposition = models.CharField("Итог", max_length=20, blank=True, db_index=True)
    dcontext = models.CharField("Контекст диалплана", max_length=80, blank=True)
    userfield = models.CharField(
        "Результат обзвона", max_length=255, blank=True,
        help_text="Служебное поле CDR: 201:NOANSWER:19|202:ANSWER:16 и т.п.",
    )

    # --- запись разговора ---
    recording = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="calls", verbose_name="Запись (mp3)",
    )
    source_path = models.CharField(
        "Путь на АТС", max_length=255, blank=True,
        help_text="Исходный wav (поле rec_name в CDR) — для сверки при разборе.",
    )
    has_recording_on_pbx = models.BooleanField(
        "На АТС есть запись", default=False,
        help_text="У неотвеченных звонков файл пустой (44 байта) и не переносится.",
    )

    OUTCOME_ANSWERED = "answered"
    OUTCOME_VOICEMAIL = "voicemail"
    OUTCOME_MISSED = "missed"
    OUTCOME_NO_ANSWER = "no_answer"
    OUTCOME_BUSY = "busy"
    OUTCOME_FAILED = "failed"
    OUTCOME_CHOICES = [
        (OUTCOME_ANSWERED, "Разговор состоялся"),
        (OUTCOME_VOICEMAIL, "Голосовое сообщение"),
        (OUTCOME_MISSED, "Пропущен"),
        (OUTCOME_NO_ANSWER, "Не ответили"),
        (OUTCOME_BUSY, "Занято"),
        (OUTCOME_FAILED, "Не состоялся"),
    ]
    outcome = models.CharField(
        "Итог звонка", max_length=12, choices=OUTCOME_CHOICES,
        blank=True, db_index=True,
        help_text="Сводный итог по всем «ногам» звонка. 🛑 Не то же, что "
                  "disposition из CDR: у звонка, ушедшего на голосовую почту, "
                  "последняя нога помечена ANSWERED, хотя с клиентом никто "
                  "не разговаривал.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Звонок"
        verbose_name_plural = "Звонки"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["client", "-started_at"]),
            models.Index(fields=["employee", "-started_at"]),
        ]

    def __str__(self):
        return f"{self.started_at:%d.%m.%Y %H:%M} {self.src} → {self.dst}"

    @property
    def is_answered(self) -> bool:
        return self.disposition == "ANSWERED"

    @property
    def billsec_human(self) -> str:
        """281 → «4:41». Разговоры бывают по полчаса, и «281 с» в таблице
        читается плохо."""
        total = int(self.billsec or 0)
        if not total:
            return ""
        minutes, sec = divmod(total, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours}:{minutes:02d}:{sec:02d}"
        return f"{minutes}:{sec:02d}"


class CallListen(models.Model):
    """Журнал прослушиваний: кто и когда открывал запись разговора.

    Записи разговоров — чувствительные данные, поэтому доступ к ним
    протоколируется. Пишется в ``views.call_recording`` на каждую выдачу
    ссылки на файл.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    call = models.ForeignKey(
        Call, on_delete=models.CASCADE, related_name="listens", verbose_name="Звонок",
        null=True, blank=True,
    )
    # Голосовое сообщение слушают из реестра пропущенных, и звонка в журнале
    # у него может не быть вовсе (обращение приехало по хуку диалплана раньше,
    # чем выгрузка CDR). Протоколировать прослушивание нужно всё равно:
    # в сообщении звучит клиент.
    missed_call = models.ForeignKey(
        "MissedCall", on_delete=models.CASCADE, related_name="listens",
        null=True, blank=True, verbose_name="Пропущенный звонок",
    )
    employee = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True,
        related_name="call_listens", verbose_name="Кто слушал",
    )
    listened_at = models.DateTimeField("Когда", auto_now_add=True, db_index=True)
    ip = models.GenericIPAddressField("IP", null=True, blank=True)

    class Meta:
        verbose_name = "Прослушивание записи"
        verbose_name_plural = "Прослушивания записей"
        ordering = ["-listened_at"]

    def __str__(self):
        target = self.call_id or self.missed_call_id
        return f"{self.employee} → {target} ({self.listened_at:%d.%m.%Y %H:%M})"


class IncomingCallAlert(models.Model):
    """Карточка «вам звонили» — висит, пока сотрудник её не уберёт.

    🛑 Живёт в базе, а не только в DOM: смысл карточки в том, что человек
    отошёл и не взял трубку. Пока это была только разметка на странице,
    любое обновление (F5) стирало напоминание — ровно в тот момент, когда
    оно нужнее всего. Теперь карточки восстанавливаются при загрузке.

    Показываем только сегодняшние: к концу рабочего дня список сам
    обнуляется, отдельная чистка не нужна.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        "core.Employee", on_delete=models.CASCADE,
        related_name="call_alerts", verbose_name="Кому звонили",
    )
    channel_key = models.CharField(
        "Ключ ноги звонка", max_length=64, db_index=True,
        help_text="DestUniqueid:внутренний — по нему приходит обновление "
                  "статуса, когда звонок завершился.",
    )
    phone = models.CharField("Номер звонящего", max_length=32, blank=True)
    client = models.ForeignKey(
        "crm.Client", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="call_alerts", verbose_name="Клиент",
    )
    started_at = models.DateTimeField("Позвонили", auto_now_add=True, db_index=True)
    answered = models.BooleanField(
        "Трубку взяли", default=False,
        help_text="Проставляется по DialEnd: ANSWER — взяли, иначе пропущен.",
    )
    finished = models.BooleanField("Звонок завершён", default=False)
    comment = models.TextField(
        "Комментарий к звонку", blank=True, default="",
        help_text="Короткая заметка прямо с карточки. Если клиент опознан, "
                  "она же уходит в его событийку.",
    )
    dismissed_at = models.DateTimeField("Убрана сотрудником", null=True, blank=True)

    class Meta:
        verbose_name = "Карточка входящего звонка"
        verbose_name_plural = "Карточки входящих звонков"
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "channel_key"],
                                    name="uniq_alert_per_leg"),
        ]

    def __str__(self):
        return f"{self.phone} → {self.employee} ({self.started_at:%d.%m %H:%M})"


class CallGroup(models.Model):
    """Направление входящего звонка: колл-центр, отдел сбора документов, юротдел.

    Справочник, а не хардкод: состав групп на АТС меняется (номера переходят
    от человека к человеку, отделы переименовываются), и правит его
    руководитель в CRM, а не программист в диалплане.

    🛑 Группа определяется ДВУМЯ способами, и оба нужны:
    - ``code`` — приходит из диалплана (``miss_call_cc`` → ``cc``), это точный
      сигнал: АТС сама знает, куда вела ветка обзвона;
    - ``extensions`` — резервный путь для звонков, пришедших из CDR без хука
      (сотрудник набрал внутренний напрямую, звонок брошен в IVR).
    """

    code = models.CharField(
        "Код", max_length=16, unique=True,
        help_text="Тот же код, что диалплан передаёт скриптом уведомления: "
                  "cc (колл-центр), osd (сбор документов), yuro (юротдел).",
    )
    name = models.CharField("Название", max_length=120)
    extensions = models.CharField(
        "Внутренние номера", max_length=120, blank=True, default="",
        help_text="Через запятую: 201,202. По ним группа определяется у "
                  "звонков, пришедших без сигнала от диалплана.",
    )
    department = models.ForeignKey(
        "core.Department", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="call_groups", verbose_name="Отдел",
    )
    notify_department = models.BooleanField(
        "Уведомлять весь отдел", default=True,
        help_text="Слать пропущенные всем активным сотрудникам отдела.",
    )
    notify_management = models.BooleanField(
        "Уведомлять руководство", default=False,
        help_text="Дополнительно слать руководителям (admin / head_dep / "
                  "managing_partner).",
    )
    subscribers = models.ManyToManyField(
        "core.Employee", blank=True, related_name="call_groups",
        verbose_name="Дополнительные подписчики",
        help_text="Кому слать сверх отдела — например, РОПу из другого отдела.",
    )
    is_active = models.BooleanField("Активна", default=True)

    class Meta:
        verbose_name = "Группа входящих звонков"
        verbose_name_plural = "Группы входящих звонков"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def extension_list(self) -> list:
        return [e.strip() for e in (self.extensions or "").split(",") if e.strip()]


class MissedCall(models.Model):
    """Пропущенный входящий — обращение, на которое никто не ответил.

    Заменяет письма с АТС (``sc_send_missed.sh`` / ``sc_send_ticket_*.sh``):
    почта с АТС не уходит с тех пор, как mail.ru отрезал SMTP на их тарифе,
    и все обращения терялись молча.

    🛑 Ключ идемпотентности — ``linkedid`` (склейка ног одного звонка), потому
    что запись создают ДВА независимых источника:
    1) диалплан по горячим следам (мгновенное уведомление),
    2) выгрузка CDR агентом раз в 5 минут (страховка, если АТС не достучалась
       до CRM, и единственный путь для звонков, брошенных в IVR).
    Оба знают linkedid, поэтому второй источник дополняет запись, а не двоит её.
    """

    KIND_MISSED = "missed"
    KIND_VOICEMAIL = "voicemail"
    KIND_IVR = "ivr"
    KIND_CHOICES = [
        (KIND_MISSED, "Никто не ответил"),
        (KIND_VOICEMAIL, "Голосовое сообщение"),
        (KIND_IVR, "Бросил трубку в меню"),
    ]

    STATUS_NEW = "new"
    STATUS_IN_WORK = "in_work"
    STATUS_DONE = "done"
    STATUS_AUTO_DONE = "auto_done"
    STATUS_IGNORED = "ignored"
    STATUS_CHOICES = [
        (STATUS_NEW, "Новый"),
        (STATUS_IN_WORK, "В работе"),
        (STATUS_DONE, "Отработан"),
        (STATUS_AUTO_DONE, "Связались (автоматически)"),
        (STATUS_IGNORED, "Не требует ответа"),
    ]
    OPEN_STATUSES = (STATUS_NEW, STATUS_IN_WORK)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    linkedid = models.CharField(
        "Linkedid", max_length=32, unique=True,
        help_text="Ключ звонка целиком (все его ноги) — по нему запись "
                  "дополняется вторым источником, а не дублируется.",
    )
    uniqueid = models.CharField("Uniqueid", max_length=32, blank=True)
    call = models.ForeignKey(
        Call, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="missed_records", verbose_name="Звонок в журнале",
    )

    occurred_at = models.DateTimeField("Когда звонили", db_index=True)
    kind = models.CharField(
        "Тип", max_length=10, choices=KIND_CHOICES, default=KIND_MISSED, db_index=True)
    group = models.ForeignKey(
        CallGroup, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="missed_calls", verbose_name="Куда звонили",
    )
    extension = models.CharField("Внутренний номер", max_length=8, blank=True, default="")

    phone = models.CharField(
        "Номер звонившего", max_length=32, blank=True, db_index=True,
        help_text="Нормализованный вид — по нему ищется клиент.",
    )
    raw_phone = models.CharField(
        "Номер как пришёл с АТС", max_length=40, blank=True, default="")
    client = models.ForeignKey(
        "crm.Client", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="missed_calls", verbose_name="Клиент",
    )

    recording = models.ForeignKey(
        "files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="voicemails", verbose_name="Голосовое сообщение (mp3)",
    )
    voicemail_seconds = models.IntegerField("Длительность сообщения, с", default=0)
    voicemail_file = models.CharField(
        "Файл на АТС", max_length=255, blank=True, default="",
        help_text="Имя wav в /var/spool/asterisk/monitor — по нему агент "
                  "дошлёт запись после того, как MixMonitor её закроет.",
    )

    status = models.CharField(
        "Статус", max_length=10, choices=STATUS_CHOICES,
        default=STATUS_NEW, db_index=True)
    assignee = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="missed_calls", verbose_name="Кто отрабатывает",
    )
    handled_at = models.DateTimeField("Отработан", null=True, blank=True)
    handled_by = models.ForeignKey(
        "core.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="missed_calls_handled", verbose_name="Кто закрыл",
    )
    closed_by_call = models.ForeignKey(
        Call, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="closes_missed", verbose_name="Разговор, закрывший обращение",
    )
    comment = models.TextField(
        "Заметки", blank=True, default="",
        help_text="Накапливаются построчно (HH:MM — текст), как на карточке звонка.",
    )

    notified_at = models.DateTimeField(
        "Уведомления разосланы", null=True, blank=True,
        help_text="🛑 Метка защищает от повторной рассылки: запись трогают оба "
                  "источника, а уведомление должно уйти один раз.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Пропущенный звонок"
        verbose_name_plural = "Пропущенные звонки"
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["status", "-occurred_at"]),
            models.Index(fields=["phone", "-occurred_at"]),
        ]

    def __str__(self):
        return f"{self.phone or self.raw_phone} → {self.group or '—'} ({self.occurred_at:%d.%m %H:%M})"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    @property
    def waiting_minutes(self) -> int:
        """Сколько минут обращение висит без ответа — для подсветки в реестре."""
        from django.utils import timezone as _tz
        end = self.handled_at if not self.is_open and self.handled_at else _tz.now()
        return max(0, int((end - self.occurred_at).total_seconds() // 60))

    @property
    def waiting_human(self) -> str:
        """«3 ч 12 мин» — «192 мин» в таблице читается плохо, а сутки ожидания
        по обращению клиента должны бросаться в глаза."""
        minutes = self.waiting_minutes
        if minutes < 60:
            return f"{minutes} мин"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours} ч {minutes} мин"
        days, hours = divmod(hours, 24)
        return f"{days} дн {hours} ч"
