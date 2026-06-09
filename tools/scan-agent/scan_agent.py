#!/usr/bin/env python3
"""SiriCRM scan-agent — мостик между офисным МФУ и CRM.

Следит за папкой, куда сетевой сканер кладёт PDF (например, расшаренная
SMB-папка), и заливает каждый новый файл в CRM по HTTP. CRM кладёт его в
лоток «Входящие сканы», где секретарь привязывает скан к клиенту.

По умолчанию запускается с иконкой в системном трее:
  • цвет иконки = состояние (зелёный — всё ок, оранжевый — сбой заливки,
    красный — нет связи/не настроен, серый — запуск);
  • правый клик → меню: статус, проверка связи, открыть папку,
    «Настройки…» (редактор config.ini), открыть config.ini, выход.

Без трея (Linux-служба, сервер без графики):
    python scan_agent.py --no-tray --config config.ini

Зависимости: requests, watchdog (+ pystray, Pillow для трея).
    pip install -r requirements.txt

Как служба Windows — удобнее всего через NSSM (см. README.md).
"""
import argparse
import configparser
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from watchdog.events import FileSystemEventHandler
# PollingObserver — опрос каталога листингом. В отличие от нативного Observer
# (inotify / ReadDirectoryChangesW) РАБОТАЕТ НА СЕТЕВЫХ ПАПКАХ (SMB/NFS), где
# события ФС по сети не приходят. Папка сканера почти всегда сетевая — поэтому
# поллинг тут надёжнее «событийного» наблюдателя.
from watchdog.observers.polling import PollingObserver

log = logging.getLogger("scan-agent")

# Отметка сборки — видна в логе при старте. Если в логе старая дата —
# запущен старый процесс (нужно полностью перезапустить агент, а не «Сохранить»).
AGENT_BUILD = "2026-06-08 (polling + tray + done-retention)"

# Какие расширения заливаем (сканеры обычно дают PDF, иногда TIFF/JPG).
ALLOWED_EXT = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}

# Ключи config.ini секции [agent] + дефолты.
CONFIG_FIELDS = [
    ("watch_dir", ""),
    ("intake_url", "https://siricrm.ru/scans/agent/intake/"),
    ("token", ""),
    ("device", "office-mfu"),
    ("done_dir", ""),
    ("done_retention_days", "3"),
    ("settle_seconds", "3"),
    ("poll_interval", "5"),
    ("verify_tls", "true"),
]
REQUIRED = ("watch_dir", "intake_url", "token")


# ── Конфиг ──────────────────────────────────────────────────────────────────

def read_config(config_path: str) -> dict:
    """Читает config.ini (+ env-override SCAN_<KEY>). Не падает, если файла нет."""
    cfg = configparser.ConfigParser()
    if Path(config_path).exists():
        cfg.read(config_path, encoding="utf-8")
    sect = cfg["agent"] if cfg.has_section("agent") else {}
    conf = {}
    for key, default in CONFIG_FIELDS:
        conf[key] = os.environ.get(f"SCAN_{key.upper()}") or sect.get(key, default)
    # Приведение типов
    try:
        conf["settle_seconds"] = float(conf["settle_seconds"] or 3)
    except ValueError:
        conf["settle_seconds"] = 3.0
    try:
        conf["poll_interval"] = float(conf["poll_interval"] or 5)
    except ValueError:
        conf["poll_interval"] = 5.0
    try:
        conf["done_retention_days"] = float(conf["done_retention_days"] or 0)
    except ValueError:
        conf["done_retention_days"] = 3.0
    conf["verify_tls"] = str(conf["verify_tls"]).lower() != "false"
    return conf


def write_config(config_path: str, values: dict):
    """Сохраняет значения в [agent] config.ini."""
    cfg = configparser.ConfigParser()
    if Path(config_path).exists():
        cfg.read(config_path, encoding="utf-8")
    if not cfg.has_section("agent"):
        cfg.add_section("agent")
    for key, _ in CONFIG_FIELDS:
        if key in values:
            cfg.set("agent", key, str(values[key]))
    with open(config_path, "w", encoding="utf-8") as fh:
        cfg.write(fh)


def missing_required(conf: dict) -> list:
    return [k for k in REQUIRED if not conf.get(k)]


# ── Состояние (для трея) ─────────────────────────────────────────────────────

class AppState:
    """Потокобезопасный снимок состояния агента для UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status = "starting"   # starting|ok|warn|error
        self.message = "Запуск…"
        self.uploaded = 0
        self.failed = 0
        self.last_file = ""
        self.last_ok = None        # datetime последней успешной заливки
        self._listeners = []

    def on_change(self, fn):
        self._listeners.append(fn)

    def _notify(self):
        for fn in self._listeners:
            try:
                fn()
            except Exception:
                pass

    def set(self, status, message):
        with self._lock:
            self.status = status
            self.message = message
        log.info("[%s] %s", status, message)
        self._notify()

    def bump_uploaded(self, name):
        with self._lock:
            self.uploaded += 1
            self.last_file = name
            self.last_ok = datetime.now()
            self.status = "ok"
            self.message = f"Залит: {name}"
        self._notify()

    def bump_failed(self, name, err):
        with self._lock:
            self.failed += 1
            self.status = "warn"
            self.message = f"Сбой заливки {name}: {err}"
        self._notify()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "message": self.message,
                "uploaded": self.uploaded,
                "failed": self.failed,
                "last_file": self.last_file,
                "last_ok": self.last_ok,
            }


# ── Агент: слежение + заливка ────────────────────────────────────────────────

class ScanAgent:
    def __init__(self, config_path: str, state: AppState):
        self.config_path = config_path
        self.state = state
        self.conf = read_config(config_path)
        self.observer = None
        self.seen = set()
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = None

    # --- сеть ---
    def ping(self) -> bool:
        url = self.conf.get("intake_url", "")
        if not url:
            return False
        ping_url = url.replace("/intake/", "/ping/")
        try:
            r = requests.get(
                ping_url,
                headers={"Authorization": f"Bearer {self.conf['token']}"},
                timeout=15, verify=self.conf["verify_tls"],
            )
        except requests.RequestException as e:
            self.state.set("error", f"Нет связи с CRM: {e}")
            return False
        if r.status_code == 200:
            return True
        if r.status_code in (401, 403):
            self.state.set("error", "CRM отклонила токен (проверьте token в настройках)")
        else:
            self.state.set("error", f"CRM вернула {r.status_code} на ping")
        return False

    def upload(self, path: Path) -> bool:
        try:
            with open(path, "rb") as fh:
                resp = requests.post(
                    self.conf["intake_url"],
                    headers={"Authorization": f"Bearer {self.conf['token']}"},
                    files={"file": (path.name, fh, "application/octet-stream")},
                    data={"device": self.conf["device"]},
                    timeout=120, verify=self.conf["verify_tls"],
                )
        except requests.RequestException as e:
            self.state.bump_failed(path.name, str(e))
            return False
        if resp.status_code == 201:
            self.state.bump_uploaded(path.name)
            return True
        self.state.bump_failed(path.name, f"HTTP {resp.status_code}")
        log.error("Сервер вернул %s: %s", resp.status_code, resp.text[:200])
        return False

    # --- файлы ---
    @staticmethod
    def _wait_stable(path: Path, settle: float) -> bool:
        last = -1
        for _ in range(60):
            try:
                size = path.stat().st_size
            except OSError:
                return False
            if size == last and size > 0:
                return True
            last = size
            time.sleep(settle)
        return last > 0

    def handle_file(self, path: Path):
        if path.suffix.lower() not in ALLOWED_EXT:
            log.info("Пропуск (не тот тип файла): %s", path.name)
            return
        key = str(path.resolve())
        if key in self.seen:
            return
        log.info("Обнаружен файл: %s", path.name)
        if not self._wait_stable(path, self.conf["settle_seconds"]):
            log.warning("Файл не стабилизировался, пропуск: %s", path.name)
            return
        self.seen.add(key)
        if self.upload(path):
            self._move_done(path)
        else:
            self.seen.discard(key)  # дать шанс повторить

    def _move_done(self, path: Path):
        done = self.conf.get("done_dir")
        if not done:
            return
        try:
            dest = Path(done)
            dest.mkdir(parents=True, exist_ok=True)
            path.rename(dest / path.name)
        except OSError as e:
            log.warning("Не удалось перенести %s: %s", path.name, e)

    # --- очистка папки залитых по сроку хранения ---
    def cleanup_done(self):
        """Удаляет из done_dir файлы старше done_retention_days."""
        done = self.conf.get("done_dir")
        days = self.conf.get("done_retention_days") or 0
        if not done or days <= 0:
            return
        d = Path(done)
        if not d.is_dir():
            return
        cutoff = time.time() - days * 86400
        removed = 0
        for f in d.iterdir():
            if not f.is_file():
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError as e:
                log.warning("Не удалось удалить %s: %s", f.name, e)
        if removed:
            log.info("Очистка done_dir: удалено %s файл(ов) старше %s дн.", removed, days)

    def _cleanup_loop(self):
        # Чистим сразу при старте, затем раз в час.
        while not self._cleanup_stop.is_set():
            try:
                self.cleanup_done()
            except Exception as e:
                log.warning("Сбой очистки done_dir: %s", e)
            self._cleanup_stop.wait(3600)

    # --- жизненный цикл ---
    def start(self) -> bool:
        """(Пере)читает конфиг, проверяет связь, запускает слежение."""
        self.stop()
        self.conf = read_config(self.config_path)
        miss = missing_required(self.conf)
        if miss:
            self.state.set("error", "Не заполнено: " + ", ".join(miss) + ". Откройте «Настройки…».")
            return False
        watch_dir = Path(self.conf["watch_dir"])
        if not watch_dir.is_dir():
            self.state.set("error", f"Папка не найдена: {watch_dir}")
            return False

        if not self.ping():
            # связь не ок — но всё равно начнём следить (заливки будут ретраиться)
            log.warning("Связь с CRM не установлена, всё равно слежу за папкой.")
        else:
            self.state.set("ok", f"Слежу за {watch_dir}")

        # Догоняем то, что уже лежит в папке.
        for f in sorted(watch_dir.iterdir()):
            if f.is_file():
                self.handle_file(f)

        handler = _Handler(self)
        # PollingObserver: опрашивает листинг каждые poll_interval секунд —
        # надёжно на сетевых папках, где события ФС не приходят.
        self.observer = PollingObserver(timeout=self.conf["poll_interval"])
        self.observer.schedule(handler, str(watch_dir), recursive=False)
        self.observer.start()
        if self.state.status not in ("error",):
            self.state.set("ok", f"Слежу за {watch_dir}")
        log.info("👀 Слежу за папкой (опрос каждые %ss): %s → %s",
                 self.conf["poll_interval"], watch_dir, self.conf["intake_url"])

        # (Пере)запуск фоновой очистки done_dir — ровно один живой поток.
        self._cleanup_stop.set()
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=2)
        self._cleanup_stop.clear()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        return True

    def stop(self):
        self._cleanup_stop.set()
        if self.observer is not None:
            try:
                self.observer.stop()
                self.observer.join(timeout=5)
            except Exception:
                pass
            self.observer = None

    def reload(self):
        """Перезапуск после изменения настроек."""
        self.seen.clear()
        self.start()


class _Handler(FileSystemEventHandler):
    def __init__(self, agent: ScanAgent):
        self.agent = agent

    def on_created(self, event):
        if not event.is_directory:
            self.agent.handle_file(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self.agent.handle_file(Path(event.dest_path))


# ── Утилиты ОС ───────────────────────────────────────────────────────────────

def open_path(path: str):
    """Открыть файл/папку в проводнике/редакторе ОС."""
    if not path:
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: P204
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as e:
        log.warning("Не удалось открыть %s: %s", path, e)


# ── Окно настроек (tkinter) ──────────────────────────────────────────────────

def open_settings_window(agent: ScanAgent):
    import tkinter as tk
    from tkinter import messagebox, ttk

    conf = read_config(agent.config_path)

    root = tk.Tk()
    root.title("SiriCRM scan-agent — настройки")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    rows = [
        ("watch_dir", "Папка сканера (SMB/локальная)", False),
        ("intake_url", "Адрес приёма CRM", False),
        ("token", "Токен (SCAN_AGENT_TOKEN)", True),
        ("device", "Метка устройства", False),
        ("done_dir", "Папка для залитых (необязательно)", False),
        ("done_retention_days", "Хранить залитые, дней (0 — не чистить)", False),
        ("settle_seconds", "Пауза стабилизации, сек", False),
        ("poll_interval", "Интервал опроса папки, сек", False),
    ]
    entries = {}
    frm = ttk.Frame(root, padding=12)
    frm.grid(row=0, column=0)

    for i, (key, label, secret) in enumerate(rows):
        ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
        var = tk.StringVar(value=str(conf.get(key, "")))
        ent = ttk.Entry(frm, textvariable=var, width=46, show="*" if secret else "")
        ent.grid(row=i, column=1, pady=3)
        entries[key] = var

    verify_var = tk.BooleanVar(value=bool(conf.get("verify_tls", True)))
    ttk.Checkbutton(frm, text="Проверять TLS-сертификат (вкл. для prod)",
                    variable=verify_var).grid(row=len(rows), column=1, sticky="w", pady=3)

    status_lbl = ttk.Label(frm, text="", foreground="#666")
    status_lbl.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def collect():
        vals = {k: v.get().strip() for k, v in entries.items()}
        vals["verify_tls"] = "true" if verify_var.get() else "false"
        return vals

    def do_test():
        write_config(agent.config_path, collect())
        agent.conf = read_config(agent.config_path)
        miss = missing_required(agent.conf)
        if miss:
            status_lbl.config(text="Заполните: " + ", ".join(miss), foreground="#c00")
            return
        ok = agent.ping()
        if ok:
            status_lbl.config(text="✓ Связь с CRM есть, токен принят", foreground="#080")
        else:
            status_lbl.config(text="✗ " + agent.state.snapshot()["message"], foreground="#c00")

    def do_save():
        vals = collect()
        write_config(agent.config_path, vals)
        agent.reload()
        messagebox.showinfo("Сохранено", "Настройки сохранены, агент перезапущен.")
        root.destroy()

    btns = ttk.Frame(root, padding=(12, 0, 12, 12))
    btns.grid(row=1, column=0, sticky="e")
    ttk.Button(btns, text="Проверить связь", command=do_test).grid(row=0, column=0, padx=4)
    ttk.Button(btns, text="Сохранить", command=do_save).grid(row=0, column=1, padx=4)
    ttk.Button(btns, text="Отмена", command=root.destroy).grid(row=0, column=2, padx=4)

    root.mainloop()


# ── Трей ─────────────────────────────────────────────────────────────────────

_STATUS_COLOR = {
    "ok": (40, 167, 69),       # зелёный
    "warn": (255, 153, 0),     # оранжевый
    "error": (220, 53, 69),    # красный
    "starting": (150, 150, 150),  # серый
}
_STATUS_WORD = {
    "ok": "Работает",
    "warn": "Сбой заливки",
    "error": "Проблема",
    "starting": "Запуск",
}


def _make_image(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # лист документа
    d.rectangle([14, 8, 50, 56], fill=(255, 255, 255, 255), outline=(90, 90, 90, 255), width=2)
    d.line([20, 20, 44, 20], fill=(170, 170, 170, 255), width=2)
    d.line([20, 28, 44, 28], fill=(170, 170, 170, 255), width=2)
    d.line([20, 36, 38, 36], fill=(170, 170, 170, 255), width=2)
    # индикатор состояния
    d.ellipse([34, 34, 58, 58], fill=color + (255,), outline=(255, 255, 255, 255), width=2)
    return img


def run_tray(agent: ScanAgent, state: AppState, config_path: str):
    import pystray

    def tooltip():
        s = state.snapshot()
        word = _STATUS_WORD.get(s["status"], s["status"])
        last = s["last_ok"].strftime("%H:%M:%S") if s["last_ok"] else "—"
        return (f"SiriCRM scan-agent — {word}\n"
                f"{s['message']}\n"
                f"Залито: {s['uploaded']} · сбоев: {s['failed']} · последний: {last}")

    def status_text(_item):
        s = state.snapshot()
        return f"● {_STATUS_WORD.get(s['status'], s['status'])} — залито {s['uploaded']}"

    def on_test(icon, item):
        threading.Thread(target=agent.ping, daemon=True).start()

    def on_open_folder(icon, item):
        open_path(agent.conf.get("watch_dir", ""))

    def on_settings(icon, item):
        threading.Thread(target=open_settings_window, args=(agent,), daemon=True).start()

    def on_open_config(icon, item):
        open_path(config_path)

    def on_open_log(icon, item):
        open_path(log_path_for(config_path))

    def on_recheck(icon, item):
        threading.Thread(target=agent.reload, daemon=True).start()

    def on_quit(icon, item):
        agent.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Проверить связь", on_test),
        pystray.MenuItem("Перезапустить слежение", on_recheck),
        pystray.MenuItem("Открыть папку сканов", on_open_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Настройки…", on_settings, default=True),
        pystray.MenuItem("Открыть config.ini", on_open_config),
        pystray.MenuItem("Открыть лог", on_open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", on_quit),
    )

    icon = pystray.Icon(
        "siri-scan-agent",
        _make_image(_STATUS_COLOR["starting"]),
        tooltip(), menu,
    )

    def refresh():
        icon.icon = _make_image(_STATUS_COLOR.get(state.snapshot()["status"], _STATUS_COLOR["starting"]))
        icon.title = tooltip()
        try:
            icon.update_menu()
        except Exception:
            pass

    state.on_change(refresh)

    # Запуск слежения в фоне, чтобы трей отрисовался сразу.
    def boot():
        agent.start()
        refresh()
    threading.Thread(target=boot, daemon=True).start()

    icon.run()


# ── Headless (Linux-служба / без графики) ────────────────────────────────────

def run_headless(agent: ScanAgent):
    if not agent.start():
        # подождём и попробуем ещё — вдруг папка примонтируется
        while not agent.start():
            time.sleep(30)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        agent.stop()


# ── main ─────────────────────────────────────────────────────────────────────

def log_path_for(config_path: str) -> str:
    return str(Path(config_path).resolve().with_name("scan-agent.log"))


def setup_logging(config_path: str):
    """Лог в консоль + в файл рядом с config.ini (под pythonw консоли нет)."""
    from logging.handlers import RotatingFileHandler
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        fh = RotatingFileHandler(
            log_path_for(config_path), maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:  # сетевой путь к логу недоступен — не критично
        log.warning("Не удалось открыть лог-файл: %s", e)


def main():
    p = argparse.ArgumentParser(description="SiriCRM scan-agent")
    p.add_argument("--config", default="config.ini", help="Путь к config.ini")
    p.add_argument("--no-tray", action="store_true", help="Без трея (Linux-служба)")
    args = p.parse_args()

    setup_logging(args.config)
    log.info("=== SiriCRM scan-agent запущен: сборка %s, config=%s ===",
             AGENT_BUILD, Path(args.config).resolve())

    state = AppState()
    agent = ScanAgent(args.config, state)

    use_tray = not args.no_tray
    if use_tray:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            log.warning("pystray/Pillow не установлены — запуск без трея. "
                        "Поставьте: pip install -r requirements.txt")
            use_tray = False
        if use_tray and not sys.platform.startswith("win") and not os.environ.get("DISPLAY"):
            log.warning("Нет графической сессии (DISPLAY) — запуск без трея.")
            use_tray = False

    if use_tray:
        run_tray(agent, state, args.config)
    else:
        run_headless(agent)


if __name__ == "__main__":
    main()
