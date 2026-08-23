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
        return f"{self.employee} → {self.call_id} ({self.listened_at:%d.%m.%Y %H:%M})"
