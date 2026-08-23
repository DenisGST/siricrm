"""Слушатель событий АТС: всплывашка у сотрудника при входящем звонке.

Долгоживущий процесс (отдельный контейнер `pbx-listener`, как `userbot`):
держит соединение с AMI и на каждое `DialBegin` в сторону внутреннего номера
показывает владельцу этого номера карточку звонящего.

🛑 Соединение с АТС — только через WireGuard-туннель. Если `PBX_AMI_HOST`
пуст, команда мирно выходит: на dev телефонии нет, и контейнер не должен
падать в перезапуск по кругу.

    python manage.py pbx_ami_listen
"""
import logging
import re
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.telephony.ami import AmiClient, AmiError
from apps.telephony.notifications import push_call_ended, push_incoming_call
from apps.telephony.services import is_extension, normalize_counterparty, resolve_client

logger = logging.getLogger(__name__)

# PJSIP/201-00000abc → 201
CHANNEL_EXT_RE = re.compile(r"^PJSIP/(\d{3})-")

RECONNECT_MIN = 5
RECONNECT_MAX = 120


class Command(BaseCommand):
    help = "Слушать события Asterisk AMI и показывать всплывашки о входящих"

    def handle(self, *args, **opts):
        self._stop = False
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        if not getattr(settings, "PBX_AMI_HOST", ""):
            self.stdout.write(self.style.WARNING(
                "PBX_AMI_HOST не задан — телефония на этом сервере выключена, выхожу."))
            return

        delay = RECONNECT_MIN
        while not self._stop:
            try:
                self._run_once()
                delay = RECONNECT_MIN          # успешная сессия — сбрасываем паузу
            except (AmiError, OSError) as exc:
                if self._stop:
                    break
                logger.warning("связь с АТС потеряна (%s), повтор через %d с", exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX)   # не долбим упавшую АТС
        self.stdout.write("слушатель остановлен")

    def _on_signal(self, *_):
        self._stop = True

    def _run_once(self):
        client = AmiClient(timeout=60)
        with client as ami:
            self.stdout.write(self.style.SUCCESS(
                f"подключён к АТС {ami.host}:{ami.port}, жду события"))
            # Таймаут на чтение нужен, чтобы замечать SIGTERM и обрыв связи.
            ami.sock.settimeout(30)
            while not self._stop:
                try:
                    packet = ami.read_packet()
                except OSError as exc:
                    if getattr(exc, "errno", None) is None and "timed out" in str(exc):
                        continue          # тишина в эфире — это нормально
                    raise
                self._handle(packet)

    # ── обработка событий ────────────────────────────────────────────────
    def _handle(self, packet: dict):
        event = packet.get("Event")
        if event == "DialBegin":
            self._on_dial_begin(packet)
        elif event == "DialEnd":
            self._on_dial_end(packet)

    def _on_dial_begin(self, p: dict):
        extension = self._dest_extension(p)
        if not extension:
            return
        caller = (p.get("CallerIDNum") or "").strip()
        # Внутренние звонки коллег всплывашкой не сопровождаем — это шум.
        if not caller or is_extension(caller):
            return

        employee = self._employee_for(extension)
        if employee is None:
            return

        phone = normalize_counterparty(caller)
        client = resolve_client(phone) if phone else None
        push_incoming_call(
            employee,
            channel_key=self._key(p),
            phone=phone or caller,
            client=client,
        )
        logger.info("входящий %s → вн.%s (%s)", caller, extension,
                    client or "клиент не найден")

    def _on_dial_end(self, p: dict):
        extension = self._dest_extension(p)
        if not extension:
            return
        employee = self._employee_for(extension)
        if employee is not None:
            push_call_ended(employee, channel_key=self._key(p))

    @staticmethod
    def _dest_extension(p: dict) -> str:
        m = CHANNEL_EXT_RE.match(p.get("DestChannel") or "")
        return m.group(1) if m else ""

    @staticmethod
    def _key(p: dict) -> str:
        """Ключ карточки: пара «нога звонка + внутренний номер».

        При параллельном обзвоне (201 и 202 одновременно) события идут с общим
        Uniqueid, но разными DestChannel — иначе ответ одного сотрудника гасил
        бы карточку у второго.
        """
        return (p.get("DestUniqueid") or p.get("Uniqueid") or "") + ":" + \
               (Command._dest_extension(p) or "?")

    @staticmethod
    def _employee_for(extension: str):
        from apps.core.models import Employee
        return (Employee.objects.filter(sip_extension=extension, user__is_active=True)
                .select_related("user").first())
