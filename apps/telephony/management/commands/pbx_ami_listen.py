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
from apps.callcenter.calls import open_prompt
from apps.callcenter.intake import handle_unknown_incoming_call
from apps.telephony.notifications import finish_incoming_call, register_incoming_call
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

    def _say(self, text: str):
        """🛑 Пишем в stdout контейнера, а не через logging: в проекте нет
        корневого логгера (настроены только django/celery/userbot/maxbot),
        поэтому logger.info из apps.* не виден нигде — пустой лог вводил
        в заблуждение при разборе «почему нет всплывашки»."""
        self.stdout.write(text)
        self.stdout.flush()

    def _on_dial_begin(self, p: dict):
        dest = p.get("DestChannel") or ""
        extension = self._dest_extension(p)
        caller = (p.get("CallerIDNum") or p.get("ConnectedLineNum") or "").strip()

        if not extension:
            self._say(f"DialBegin: не внутренний номер (DestChannel={dest!r}) — пропуск")
            return
        # Внутренние звонки коллег всплывашкой не сопровождаем — это шум.
        if not caller:
            self._say(f"DialBegin → вн.{extension}: нет CallerIDNum — пропуск")
            return
        if is_extension(caller):
            self._say(f"DialBegin → вн.{extension}: звонит коллега {caller} — пропуск")
            return

        employee = self._employee_for(extension)
        if employee is None:
            self._say(f"DialBegin {caller} → вн.{extension}: "
                      f"нет сотрудника с таким sip_extension — пропуск")
            return

        phone = normalize_counterparty(caller)
        client = resolve_client(phone) if phone else None
        if client is None and phone:
            # Номера нет в базе — заводим неидентифицированного клиента и
            # ставим его карточку на доску колл-центра, пока телефон ещё
            # звонит. Источник выключен (нет колонки-приёмника) — вернётся
            # None, и всё остаётся как было.
            # 🛑 …or resolve_client: при параллельном обзвоне (201 и 202
            # звонят разом) клиента заводит ПЕРВАЯ нога, а остальным intake
            # честно отвечает «не моё дело» — без повторного поиска у них
            # всплывашка осталась бы без клиента.
            client = handle_unknown_incoming_call(
                phone, clid_name=(p.get("CallerIDName") or "")
            ) or resolve_client(phone)
            if client is not None:
                self._say(f"неизвестный номер {phone} → заведён клиент "
                          f"{client.pk} и карточка на доске колл-центра")
        register_incoming_call(
            employee,
            channel_key=self._key(p),
            phone=phone or caller,
            client=client,
        )
        self._say(f"ВХОДЯЩИЙ {caller} → вн.{extension} ({employee}), "
                  f"клиент: {client or 'не найден'} — всплывашка отправлена")

    def _on_dial_end(self, p: dict):
        """Звонок завершился — отмечаем итог и спрашиваем результат.

        🛑 Карточку «вам звонили» НЕ убираем: она должна висеть, пока
        сотрудник сам её не уберёт — человек мог отойти и, вернувшись, должен
        увидеть, что ему звонили. Меняется только вид — неотвеченный
        становится красным «Пропущенный звонок».

        Два случая. ВХОДЯЩИЙ — внутренний номер в DestChannel (кому звонили).
        ИСХОДЯЩИЙ — внутренний номер в Channel (кто звонит), а назначение
        внешнее; на такие события всплывашка не рисуется, но результат
        разговора нужен так же, как по входящему.
        """
        status = (p.get("DialStatus") or "").strip().upper()
        answered = status == "ANSWER"

        extension = self._dest_extension(p)
        if extension:
            employee = self._employee_for(extension)
            if employee is None:
                return
            alert = finish_incoming_call(employee, channel_key=self._key(p),
                                         answered=answered)
            if alert is not None:
                self._say(f"DialEnd вн.{extension}: {status or '—'} → "
                          f"{'принят' if alert.answered else 'ПРОПУЩЕН'}")
            self._ask_result(employee, p, direction="in", answered=answered,
                             raw_phone=p.get("CallerIDNum") or "",
                             key=self._key(p))
            return

        # Исходящий: внутренний номер — источник звонка.
        src_ext = self._src_extension(p)
        if not src_ext:
            return
        employee = self._employee_for(src_ext)
        if employee is None:
            return
        # Кому звонили: у исходящей ноги это назначение набора.
        raw = (p.get("DialString") or p.get("ConnectedLineNum")
               or p.get("DestCallerIDNum") or "")
        if is_extension(raw):
            return          # звонок коллеге — не работа с клиентом
        self._ask_result(employee, p, direction="out", answered=answered,
                         raw_phone=raw,
                         key=(p.get("Uniqueid") or "") + ":" + src_ext)

    def _ask_result(self, employee, p: dict, *, direction: str, answered: bool,
                    raw_phone: str, key: str):
        """Показать оператору модалку «Результат звонка»."""
        phone = normalize_counterparty(raw_phone)
        outcome = open_prompt(
            employee, channel_key=key, direction=direction,
            phone=phone or raw_phone, client=resolve_client(phone) if phone else None,
            answered=answered,
        )
        if outcome is not None:
            self._say(f"результат звонка ({direction}) запрошен у {employee}: "
                      f"{phone or raw_phone or '—'}")

    @staticmethod
    def _dest_extension(p: dict) -> str:
        m = CHANNEL_EXT_RE.match(p.get("DestChannel") or "")
        return m.group(1) if m else ""

    @staticmethod
    def _src_extension(p: dict) -> str:
        """Внутренний номер ЗВОНЯЩЕГО — по нему опознаём исходящий звонок."""
        m = CHANNEL_EXT_RE.match(p.get("Channel") or "")
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
