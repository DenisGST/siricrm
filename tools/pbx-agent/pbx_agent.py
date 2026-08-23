#!/usr/bin/env python3
"""Агент на сервере Asterisk: переносит журнал звонков и записи в SiriCRM.

Живёт на самой АТС (она за NAT — ходить к ней снаружи некуда, поэтому пушим
отсюда). Делает три вещи:

  1. читает новые строки из таблицы ``cdr`` и шлёт метаданные звонков в CRM;
  2. конвертирует wav→mp3 (``lame``) и заливает запись в CRM;
  3. удаляет старые wav, которые уже уехали (ретенция).

🛑 Ключей S3 здесь нет и быть не должно: файл уходит в CRM, а в хранилище его
кладёт уже она. Единственный секрет на этой машине — узкий Bearer-токен,
который умеет только принимать звонки.

Состояние — в SQLite рядом с агентом. Таблицы АТС не трогаем: колонка
``cdr.recconverted`` выглядит подходящей, но она чужая, и кто её ещё читает —
неизвестно.

Запуск:
    pbx_agent.py --once                # один проход (для systemd-таймера)
    pbx_agent.py --backfill            # перенести всю историю и выйти
    pbx_agent.py --once --limit 20     # для проверки
"""
import argparse
import configparser
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

try:
    import pymysql
    import requests
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Нет зависимости: {exc}. Нужны python3-PyMySQL и python3-requests.")

DEFAULT_CONFIG = "/etc/pbx-agent.conf"

# Пустой wav у неотвеченного звонка — 44 байта (только заголовок). Всё, что
# меньше килобайта, это доли секунды тишины: переносить нечего.
MIN_WAV_BYTES = 1024
# Не трогаем файл, пока MixMonitor может его дописывать.
MIN_AGE_SECONDS = 60
# Сколько раз пытаться перенести одну запись. Сетевые сбои лечатся повтором,
# а битый wav иначе будет шуметь в логе на каждом проходе вечно.
MAX_ATTEMPTS = 5

log = logging.getLogger("pbx-agent")


# --------------------------------------------------------------------------
# конфигурация и состояние
# --------------------------------------------------------------------------
def load_config(path):
    if not os.path.exists(path):
        sys.exit(f"Нет файла конфигурации {path} (см. pbx-agent.conf.example)")
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    c = cp["agent"]
    cfg = {
        "crm_url": c.get("crm_url").rstrip("/"),
        "token": c.get("token"),
        "db_host": c.get("db_host", fallback="127.0.0.1"),
        "db_user": c.get("db_user"),
        "db_password": c.get("db_password"),
        "db_name": c.get("db_name", fallback="asterisk"),
        "records_root": c.get("records_root", fallback="/records"),
        "state_db": c.get("state_db", fallback="/var/lib/pbx-agent/state.db"),
        "bitrate": c.getint("bitrate", fallback=24),
        "retention_days": c.getint("retention_days", fallback=30),
        "batch": c.getint("batch", fallback=200),
        "timeout": c.getint("timeout", fallback=120),
    }
    if not cfg["crm_url"] or not cfg["token"]:
        sys.exit("В конфигурации обязательны crm_url и token")
    return cfg


def open_state(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS calls(
        uniqueid    TEXT PRIMARY KEY,
        meta_sent   INTEGER DEFAULT 0,
        file_sent   INTEGER DEFAULT 0,
        wav_path    TEXT,
        wav_deleted INTEGER DEFAULT 0,
        attempts    INTEGER DEFAULT 0,
        last_error  TEXT,
        updated_at  TEXT
    )""")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_file ON calls(file_sent, meta_sent)")
    conn.commit()
    return conn


def state_get(conn, key, default=""):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def state_set(conn, key, value):
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (key, str(value)))
    conn.commit()


# --------------------------------------------------------------------------
# работа с CRM
# --------------------------------------------------------------------------
class Crm:
    def __init__(self, cfg):
        self.base = cfg["crm_url"]
        self.timeout = cfg["timeout"]
        self.s = requests.Session()
        self.s.headers["Authorization"] = "Bearer " + cfg["token"]

    def ping(self):
        r = self.s.get(f"{self.base}/telephony/agent/ping/", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def send_calls(self, rows):
        r = self.s.post(f"{self.base}/telephony/agent/calls/",
                        json={"calls": rows}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def send_recording(self, uniqueid, mp3_path, filename):
        with open(mp3_path, "rb") as fh:
            r = self.s.post(
                f"{self.base}/telephony/agent/recording/",
                data={"uniqueid": uniqueid},
                files={"file": (filename, fh, "audio/mpeg")},
                timeout=self.timeout,
            )
        if r.status_code == 404:
            return None  # звонок ещё не принят — попробуем в следующий проход
        r.raise_for_status()
        return r.json()


# --------------------------------------------------------------------------
# шаг 1: метаданные звонков
# --------------------------------------------------------------------------
CDR_FIELDS = ("id", "uniqueid", "linkedid", "calldate", "clid", "src", "dst",
              "dcontext", "duration", "billsec", "disposition", "userfield", "rec_name")


def wav_info(cfg, rec_name):
    """→ (существует и не пустой, абсолютный путь). ``rec_name`` в CDR лежит
    абсолютным путём вида /records/2026/08/21-16_12_10-301-8926….wav."""
    if not rec_name:
        return False, ""
    path = rec_name if rec_name.startswith("/") else os.path.join(cfg["records_root"], rec_name)
    try:
        st = os.stat(path)
    except OSError:
        return False, path
    return (st.st_size >= MIN_WAV_BYTES), path


def sync_meta(cfg, conn, crm, db, limit=None):
    """Новые строки CDR → CRM. Водяной знак — автоинкрементный ``cdr.id``."""
    last_id = int(state_get(conn, "last_cdr_id", "0") or 0)
    batch = cfg["batch"] if limit is None else min(cfg["batch"], limit)
    total_sent = 0

    while True:
        with db.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(CDR_FIELDS)} FROM cdr WHERE id > %s ORDER BY id LIMIT %s",
                (last_id, batch),
            )
            rows = cur.fetchall()
        if not rows:
            break

        payload, local = [], []
        for r in rows:
            has_rec, path = wav_info(cfg, r["rec_name"])
            payload.append({
                "uniqueid": r["uniqueid"],
                "linkedid": r["linkedid"] or "",
                "calldate": r["calldate"].strftime("%Y-%m-%d %H:%M:%S") if r["calldate"] else "",
                "clid": r["clid"] or "",
                "src": r["src"] or "",
                "dst": r["dst"] or "",
                "dcontext": r["dcontext"] or "",
                "duration": r["duration"] or 0,
                "billsec": r["billsec"] or 0,
                "disposition": r["disposition"] or "",
                "userfield": r["userfield"] or "",
                "rec_name": r["rec_name"] or "",
                "has_recording": has_rec,
            })
            local.append((r["uniqueid"], path if has_rec else ""))

        resp = crm.send_calls(payload)
        if resp.get("failed"):
            log.warning("CRM отклонила %d звонков, первый: %s",
                        len(resp["failed"]), resp["failed"][0])

        now = datetime.now().isoformat(timespec="seconds")
        conn.executemany(
            """INSERT INTO calls(uniqueid, meta_sent, file_sent, wav_path, updated_at)
               VALUES(?, 1, ?, ?, ?)
               ON CONFLICT(uniqueid) DO UPDATE SET meta_sent=1, wav_path=excluded.wav_path,
                                                   updated_at=excluded.updated_at""",
            [(u, 0 if p else 1, p, now) for (u, p) in local],
        )
        conn.commit()

        last_id = rows[-1]["id"]
        state_set(conn, "last_cdr_id", last_id)
        total_sent += len(rows)
        log.info("метаданные: отправлено %d (создано %s, обновлено %s), cdr.id=%s",
                 len(rows), resp.get("created"), resp.get("updated"), last_id)

        if limit is not None and total_sent >= limit:
            break
        if len(rows) < batch:
            break
    return total_sent


# --------------------------------------------------------------------------
# шаг 2: записи разговоров
# --------------------------------------------------------------------------
def convert_to_mp3(wav_path, bitrate):
    fd, mp3_path = tempfile.mkstemp(suffix=".mp3", prefix="pbx-")
    os.close(fd)
    cmd = ["lame", "--quiet", "-m", "m", "-b", str(bitrate), wav_path, mp3_path]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0 or not os.path.getsize(mp3_path):
        os.unlink(mp3_path)
        raise RuntimeError(f"lame: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    return mp3_path


def sync_recordings(cfg, conn, crm, limit=None):
    rows = conn.execute(
        """SELECT uniqueid, wav_path FROM calls
           WHERE meta_sent=1 AND file_sent=0 AND wav_path<>'' AND attempts < ?
           ORDER BY attempts, updated_at LIMIT ?""",
        (MAX_ATTEMPTS, limit or 10000),
    ).fetchall()

    sent = skipped = failed = 0
    for uniqueid, wav_path in rows:
        try:
            st = os.stat(wav_path)
        except OSError:
            # Файла нет — помечаем, чтобы не долбиться в него каждый проход.
            _mark(conn, uniqueid, file_sent=1, error="wav отсутствует")
            skipped += 1
            continue
        if st.st_size < MIN_WAV_BYTES:
            _mark(conn, uniqueid, file_sent=1, error="пустая запись")
            skipped += 1
            continue
        if time.time() - st.st_mtime < MIN_AGE_SECONDS:
            continue  # возможно, ещё пишется — вернёмся в следующий проход

        mp3_path = None
        try:
            mp3_path = convert_to_mp3(wav_path, cfg["bitrate"])
            name = os.path.basename(wav_path).rsplit(".", 1)[0] + ".mp3"
            resp = crm.send_recording(uniqueid, mp3_path, name)
            if resp is None:
                log.warning("звонок %s ещё не принят CRM — отложено", uniqueid)
                continue
            _mark(conn, uniqueid, file_sent=1, error="")
            sent += 1
        except Exception as exc:
            log.warning("запись %s не ушла: %s", uniqueid, exc)
            _mark(conn, uniqueid, error=str(exc)[:300], bump_attempt=True)
            failed += 1
        finally:
            if mp3_path and os.path.exists(mp3_path):
                os.unlink(mp3_path)

    if sent or failed or skipped:
        log.info("записи: отправлено %d, пропущено %d, ошибок %d", sent, skipped, failed)
    return sent


def _mark(conn, uniqueid, file_sent=None, error=None, bump_attempt=False):
    sets, args = ["updated_at=?"], [datetime.now().isoformat(timespec="seconds")]
    if bump_attempt:
        sets.append("attempts=attempts+1")
    if file_sent is not None:
        sets.append("file_sent=?")
        args.append(file_sent)
    if error is not None:
        sets.append("last_error=?")
        args.append(error)
    args.append(uniqueid)
    conn.execute(f"UPDATE calls SET {', '.join(sets)} WHERE uniqueid=?", args)
    conn.commit()


# --------------------------------------------------------------------------
# шаг 3: ретенция
# --------------------------------------------------------------------------
def cleanup(cfg, conn, dry_run=False):
    """Удаляем wav, которые точно уехали в CRM и старше retention_days.

    🛑 Удаляем ТОЛЬКО подтверждённое (file_sent=1 и запись реально ушла):
    пустые и сбойные помечены тем же флагом, поэтому дополнительно проверяем,
    что ошибок не было.
    """
    horizon = time.time() - cfg["retention_days"] * 86400
    rows = conn.execute(
        """SELECT uniqueid, wav_path FROM calls
           WHERE file_sent=1 AND wav_deleted=0 AND wav_path<>''
             AND (last_error IS NULL OR last_error='')""",
    ).fetchall()

    freed = removed = 0
    for uniqueid, wav_path in rows:
        try:
            st = os.stat(wav_path)
        except OSError:
            conn.execute("UPDATE calls SET wav_deleted=1 WHERE uniqueid=?", (uniqueid,))
            continue
        if st.st_mtime > horizon:
            continue
        if not dry_run:
            try:
                os.unlink(wav_path)
            except OSError as exc:
                log.warning("не удалось удалить %s: %s", wav_path, exc)
                continue
            conn.execute("UPDATE calls SET wav_deleted=1 WHERE uniqueid=?", (uniqueid,))
        freed += st.st_size
        removed += 1
    conn.commit()
    if removed:
        log.info("%s %d wav, освобождено %.1f ГБ",
                 "нашлось к удалению:" if dry_run else "удалено:", removed, freed / 2**30)
    return removed


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Перенос звонков и записей с Asterisk в SiriCRM")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    ap.add_argument("--backfill", action="store_true", help="перенести всю историю")
    ap.add_argument("--limit", type=int, help="ограничить число звонков (для проверки)")
    ap.add_argument("--no-cleanup", action="store_true", help="не удалять старые wav")
    ap.add_argument("--no-recordings", action="store_true",
                    help="перенести только журнал звонков, без файлов записей")
    ap.add_argument("--cleanup-dry-run", action="store_true", help="показать, что удалилось бы")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = load_config(args.config)
    conn = open_state(cfg["state_db"])
    crm = Crm(cfg)

    try:
        crm.ping()
    except Exception as exc:
        sys.exit(f"CRM недоступна или токен не принят: {exc}")

    db = pymysql.connect(
        host=cfg["db_host"], user=cfg["db_user"], password=cfg["db_password"],
        database=cfg["db_name"], charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )

    try:
        if args.cleanup_dry_run:
            cleanup(cfg, conn, dry_run=True)
            return
        if args.backfill:
            log.info("перенос истории: поехали")
            while True:
                n_meta = sync_meta(cfg, conn, crm, db)
                n_files = 0 if args.no_recordings else sync_recordings(cfg, conn, crm, limit=200)
                if not n_meta and not n_files:
                    break
                time.sleep(1)  # не выжираем CPU АТС целиком
            log.info("перенос истории завершён")
        else:
            sync_meta(cfg, conn, crm, db, limit=args.limit)
            if not args.no_recordings:
                sync_recordings(cfg, conn, crm, limit=args.limit)
        if not args.no_cleanup:
            cleanup(cfg, conn)
    finally:
        db.close()
        conn.close()


if __name__ == "__main__":
    main()
