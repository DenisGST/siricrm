"""Минимальный клиент Asterisk Manager Interface.

Написан вручную, а не взят библиотекой, сознательно: AMI — простой строчный
протокол поверх TCP, а новая зависимость в requirements.txt означала бы на
проде `rebuild` вместо `deploy` (Django импортирует INSTALLED_APPS при старте
и падает на отсутствующем пакете).

🛑 AMI передаёт логин, пароль и события ОТКРЫТЫМ ТЕКСТОМ. Подключаемся только
к адресу WireGuard-туннеля (`PBX_AMI_HOST`, по умолчанию 10.77.0.2) — наружу
он не смотрит.
"""
import logging
import socket
import time

from django.conf import settings

logger = logging.getLogger(__name__)

CRLF = "\r\n"
TERMINATOR = CRLF + CRLF


class AmiError(RuntimeError):
    pass


class AmiClient:
    """Синхронный клиент. Годится и для разовой команды (звонок по клику),
    и для долгого чтения событий (слушатель всплывашек)."""

    def __init__(self, host=None, port=None, username=None, secret=None, timeout=10):
        self.host = host or getattr(settings, "PBX_AMI_HOST", "")
        self.port = int(port or getattr(settings, "PBX_AMI_PORT", 5038))
        self.username = username or getattr(settings, "PBX_AMI_USERNAME", "")
        self.secret = secret or getattr(settings, "PBX_AMI_SECRET", "")
        self.timeout = timeout
        self.sock = None
        self._buf = ""
        self._packet = {}   # недособранный пакет между вызовами
        self._action_id = 0

    # ── соединение ────────────────────────────────────────────────────────
    def is_configured(self) -> bool:
        return bool(self.host and self.username and self.secret)

    def connect(self):
        if not self.is_configured():
            raise AmiError("AMI не настроен: заполните PBX_AMI_HOST/USERNAME/SECRET")
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        banner = self._read_line()
        logger.debug("AMI: %s", banner.strip())
        resp = self.action("Login", Username=self.username, Secret=self.secret)
        if (resp.get("Response") or "").lower() != "success":
            raise AmiError(f"AMI не принял вход: {resp.get('Message', resp)}")
        return self

    def close(self):
        if self.sock:
            try:
                self._send("Logoff")
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # ── низкий уровень ────────────────────────────────────────────────────
    def _read_line(self) -> str:
        while CRLF not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AmiError("AMI закрыл соединение")
            self._buf += chunk.decode("utf-8", "replace")
        line, self._buf = self._buf.split(CRLF, 1)
        return line

    def _send(self, action: str, **fields):
        self._action_id += 1
        action_id = str(self._action_id)
        parts = [f"Action: {action}", f"ActionID: {action_id}"]
        for key, value in fields.items():
            if value is None:
                continue
            # Переменные канала передаются несколькими строками Variable:
            if key == "Variables" and isinstance(value, dict):
                for vk, vv in value.items():
                    parts.append(f"Variable: {vk}={vv}")
                continue
            parts.append(f"{key}: {value}")
        self.sock.sendall((CRLF.join(parts) + TERMINATOR).encode("utf-8"))
        return action_id

    def read_packet(self) -> dict:
        """Один пакет (ответ или событие) как словарь. Пустая строка — конец.

        🛑 Недособранный пакет хранится в ``self._packet``, а не в локальной
        переменной: при таймауте чтения исключение уходит наверх, и если бы
        накопленные строки жили локально, они бы потерялись — следующий вызов
        начал бы собирать пакет с середины и вернул обрывок без поля Event.
        Именно так тихо терялись события о звонках.
        """
        while True:
            line = self._read_line()
            if line == "":
                if self._packet:
                    packet, self._packet = self._packet, {}
                    return packet
                continue          # подряд идущие пустые строки пропускаем
            if ": " in line:
                key, value = line.split(": ", 1)
            elif line.endswith(":"):
                key, value = line[:-1], ""
            else:
                continue          # мусорные строки (например, «--END COMMAND--»)
            self._packet[key] = value

    def action(self, action: str, **fields) -> dict:
        """Отправить действие и дождаться ответа именно на него."""
        action_id = self._send(action, **fields)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            packet = self.read_packet()
            if packet.get("ActionID") == action_id and "Response" in packet:
                return packet
            # Пакеты событий, пришедшие между делом, здесь не нужны.
        raise AmiError(f"AMI не ответил на {action} за {self.timeout} с")

    # ── прикладное ────────────────────────────────────────────────────────
    def originate(self, extension: str, number: str, context: str,
                  caller_id: str = "", timeout_ms: int = 30000,
                  variables: dict = None) -> dict:
        """Звонок по клику: сперва поднимаем трубку сотрудника, затем АТС
        набирает клиента по обычному диалплану.

        🛑 `CallerID` обязателен: диалплан выбирает транк ПО НОМЕРУ звонящего
        (3xx/4xx/5xx → Билайн, 2xx/7xx → МТТ). Без него звонок никуда не уйдёт.
        """
        return self.action(
            "Originate",
            Channel=f"PJSIP/{extension}",
            Context=context,
            Exten=number,
            Priority=1,
            CallerID=caller_id or extension,
            Timeout=timeout_ms,
            Async="true",
            Variables=variables or {},
        )
