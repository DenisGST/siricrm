"""Фоновые задачи ядра.

monitor_health — внешний мониторинг доступности другого окружения:
GET <HEALTH_MONITOR_TARGET_URL>/health/ раз в минуту (beat), при N неудачах
подряд шлёт алёрт в MAX и Telegram, при восстановлении — «ОК».

Кросс-серверная схема: dev мониторит прод, прод мониторит dev. Так алёрт
придёт, даже если у целевого сервера ляжет весь стек (web/celery/redis), —
именно этого не хватило при инциденте 09.06.2026 (web завис на 6 ч). Задача
крутится в Celery МОНИТОРЯЩЕГО сервера и использует его собственный
redis/боты, не завися от состояния целевого.

Включается только там, где задан HEALTH_MONITOR_TARGET_URL (иначе no-op).
"""
import logging

import requests
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("core.monitor")

_FAILS_KEY = "health_monitor:fails"
_DOWN_KEY = "health_monitor:down"
_STATE_TTL = 86400  # сутки


def _send_telegram_alert(text: str) -> bool:
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(settings, "HEALTH_ALERT_TELEGRAM_CHAT_ID", "") or "").strip()
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            logger.error("health alert TG send failed: %s %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception:
        logger.exception("health alert TG send error")
        return False


def _send_max_alert(text: str) -> bool:
    token = (getattr(settings, "MAX_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(settings, "HEALTH_ALERT_MAX_CHAT_ID", "") or "").strip()
    if not token or not chat_id:
        return False
    try:
        from apps.maxchat.sender import send_max_message
        ok, _mid, err = send_max_message(access_token=token, chat_id=chat_id, text=text)
        if not ok:
            logger.error("health alert MAX send failed: %s", err)
        return ok
    except Exception:
        logger.exception("health alert MAX send error")
        return False


def _alert(text: str):
    sent_max = _send_max_alert(text)
    sent_tg = _send_telegram_alert(text)
    logger.info("health alert отправлен: MAX=%s TG=%s", sent_max, sent_tg)
    if not sent_max and not sent_tg:
        logger.error("health alert НЕ доставлен ни в MAX, ни в Telegram — проверьте "
                     "HEALTH_ALERT_MAX_CHAT_ID / HEALTH_ALERT_TELEGRAM_CHAT_ID и токены")


@shared_task
def monitor_health():
    url = (getattr(settings, "HEALTH_MONITOR_TARGET_URL", "") or "").strip()
    if not url:
        return  # мониторинг на этом сервере выключен
    label = (getattr(settings, "HEALTH_MONITOR_LABEL", "") or url).strip()
    threshold = int(getattr(settings, "HEALTH_MONITOR_FAIL_THRESHOLD", 2) or 2)
    host = (getattr(settings, "HEALTH_MONITOR_HOST", "") or "").strip()
    headers = {"Host": host} if host else {}

    healthy = False
    detail = ""
    try:
        r = requests.get(url, timeout=10, allow_redirects=True, headers=headers)
        detail = f"HTTP {r.status_code}"
        healthy = r.status_code == 200
        if not healthy:
            try:
                detail += f" {r.json()}"
            except Exception:
                pass
    except Exception as e:
        detail = f"нет ответа ({e.__class__.__name__})"

    down = bool(cache.get(_DOWN_KEY))

    if healthy:
        cache.set(_FAILS_KEY, 0, _STATE_TTL)
        if down:
            cache.set(_DOWN_KEY, False, _STATE_TTL)
            _alert(f"✅ ВОССТАНОВЛЕНО: {label} снова отвечает ({detail}).")
        return

    fails = int(cache.get(_FAILS_KEY) or 0) + 1
    cache.set(_FAILS_KEY, fails, _STATE_TTL)
    logger.warning("health monitor: %s недоступен (%s), подряд %d/%d", label, detail, fails, threshold)
    if fails >= threshold and not down:
        cache.set(_DOWN_KEY, True, _STATE_TTL)
        now = timezone.localtime().strftime("%d.%m.%Y %H:%M:%S")
        _alert(
            f"🛑 НЕДОСТУПЕН: {label}\n"
            f"Проверка: {detail}\n"
            f"Неудач подряд: {fails}\n"
            f"Время: {now} (МСК)\n"
            f"URL: {url}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram-бот мониторинга: кнопки «Статус прод» / «Статистика».
# Поллер живёт на dev (MONITOR_BOT_POLL=true), отвечает только авторизованному
# чату (HEALTH_ALERT_TELEGRAM_CHAT_ID). Данные прода берёт через прод
# DevOps-агент (status / daily_stats) — работает даже если прод-web завис.
# ─────────────────────────────────────────────────────────────────────────────
import json as _json
import time as _time

_MON_OFFSET_KEY = "monitor_bot:offset"
_MON_LOCK_KEY = "monitor_bot:lock"
_MON_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "🖥 Статус прода", "callback_data": "prod_status"}],
        [{"text": "📊 Статистика за сутки", "callback_data": "prod_stats"}],
    ]
}


def _tg_api(method: str, payload: dict):
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    if not token:
        return None
    try:
        return requests.post(
            f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20,
        )
    except Exception:
        logger.exception("monitor bot: TG %s failed", method)
        return None


def _call_prod_agent(action: str, params: dict = None, timeout: int = 45):
    """POST job в прод DevOps-агент и ждём результат. → (ok, output, error)."""
    base = (getattr(settings, "PROD_AGENT_URL", "") or "").rstrip("/")
    token = (getattr(settings, "DEVOPS_AGENT_TOKEN_PROD", "") or "").strip()
    if not base or not token:
        return False, None, "PROD_AGENT_URL / DEVOPS_AGENT_TOKEN_PROD не заданы"
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.post(f"{base}/devops/agent/jobs/", headers=hdr,
                          json={"action_type": action, "params": params or {}}, timeout=20)
    except requests.RequestException as e:
        return False, None, f"прод недоступен ({e.__class__.__name__})"
    if r.status_code >= 400:
        return False, None, f"HTTP {r.status_code}"
    job_id = (r.json() or {}).get("id")
    if not job_id:
        return False, None, "агент не вернул id задачи"
    for _ in range(max(1, timeout // 2)):
        _time.sleep(2)
        try:
            jr = requests.get(f"{base}/devops/agent/jobs/{job_id}/", headers=hdr, timeout=15).json()
        except requests.RequestException:
            continue
        if jr.get("status") == "done":
            return True, (jr.get("output") or "(пусто)"), None
        if jr.get("status") == "failed":
            return False, None, (jr.get("output") or "задача упала")
    return False, None, "таймаут ожидания агента"


def _send_prod_report(chat_id: str, action: str, wait_text: str):
    _tg_api("sendMessage", {"chat_id": chat_id, "text": wait_text})
    ok, out, err = _call_prod_agent(action)
    if ok:
        _tg_api("sendMessage", {"chat_id": chat_id, "text": f"<pre>{out[:3900]}</pre>",
                                "parse_mode": "HTML"})
    else:
        _tg_api("sendMessage", {"chat_id": chat_id, "text": f"❌ Не удалось получить данные: {err}"})


def _monitor_allowed_ids() -> set:
    """Множество chat_id, которым разрешён бот. Fail-closed: если ничего не
    задано — пустое множество (бот не отвечает НИКОМУ).

    Источник — MONITOR_BOT_ALLOWED_CHAT_IDS (через запятую); если пусто,
    берём единственный HEALTH_ALERT_TELEGRAM_CHAT_ID (тот же Каныгин)."""
    raw = (getattr(settings, "MONITOR_BOT_ALLOWED_CHAT_IDS", "") or "").strip()
    if not raw:
        raw = (getattr(settings, "HEALTH_ALERT_TELEGRAM_CHAT_ID", "") or "").strip()
    return {x.strip() for x in raw.replace(";", ",").split(",") if x.strip()}


def _handle_monitor_update(upd: dict, allowed: set):
    cb = upd.get("callback_query")
    if cb:
        chat_id = str((cb.get("message") or {}).get("chat", {}).get("id")
                      or (cb.get("from") or {}).get("id") or "")
        if chat_id not in allowed:
            logger.warning("monitor bot: callback от неавторизованного chat_id=%s — отказ", chat_id)
            _tg_api("answerCallbackQuery", {"callback_query_id": cb.get("id"),
                                            "text": "⛔ Доступ только администратору",
                                            "show_alert": True})
            return
        _tg_api("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": "Собираю…"})
        data = cb.get("data")
        if data == "prod_status":
            _send_prod_report(chat_id, "status", "🖥 Собираю статус прода…")
        elif data == "prod_stats":
            _send_prod_report(chat_id, "daily_stats", "📊 Считаю статистику за сутки…")
        return

    msg = upd.get("message")
    if msg:
        chat_id = str(msg.get("chat", {}).get("id") or "")
        if chat_id not in allowed:
            logger.warning("monitor bot: сообщение от неавторизованного chat_id=%s — отказ", chat_id)
            _tg_api("sendMessage", {"chat_id": chat_id,
                                    "text": "⛔ Бот доступен только администратору."})
            return
        _tg_api("sendMessage", {"chat_id": chat_id,
                                "text": "Мониторинг прод-сервера — выберите отчёт:",
                                "reply_markup": _MON_KEYBOARD})


def _poll_monitor_once(token: str, allowed: set):
    params = {"timeout": 20, "allowed_updates": _json.dumps(["message", "callback_query"])}
    offset = cache.get(_MON_OFFSET_KEY)
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                            params=params, timeout=30)
        updates = (resp.json() or {}).get("result", [])
    except Exception:
        logger.exception("monitor bot: getUpdates failed")
        return
    for upd in updates:
        cache.set(_MON_OFFSET_KEY, upd["update_id"] + 1, 3600)
        try:
            _handle_monitor_update(upd, allowed)
        except Exception:
            logger.exception("monitor bot: handle update failed")


# ─────────────────────────────────────────────────────────────────────────────
# VPN-мониторинг: одна проверка с dev (VPN dev+prod через одного провайдера,
# одного достаточно). Если 3 минуты подряд VPN недоступен → алёрт MAX+TG.
# ─────────────────────────────────────────────────────────────────────────────

_VPN_FAILS_KEY = "vpn_monitor:fails"
_VPN_DOWN_KEY = "vpn_monitor:down"


@shared_task
def monitor_vpn():
    """Раз в минуту проверяет VPN-туннель через vpn_status handler (тот же,
    что используется в daily-отчёте). Алёрт идёт по той же схеме что
    monitor_health: N неудач подряд → 🛑, при восстановлении → ✅.

    Запускается на dev: исходящий HTTP к Telegram/Anthropic из dev-runner идёт
    через хостовый awg0 (split-tunnel AllowedIPs покрывают эти подсети).
    Если VPN на dev живой — peer-сервер (общий с prod) тоже живой →
    отдельная проверка прода не нужна (см. диалог 27.06.2026).

    Гейт MONITOR_BOT_POLL: на проде миграция PeriodicTask тоже создаст запись,
    но без флага задача no-op — иначе будем дублировать алёрты.
    """
    if not getattr(settings, "MONITOR_BOT_POLL", False):
        return
    threshold = int(getattr(settings, "VPN_MONITOR_FAIL_THRESHOLD", 3) or 3)

    # Запускаем тот же handler что использует ручная проверка / отчёт —
    # один и тот же код, никаких расхождений диагнозов.
    try:
        from apps.devops.handlers.vpn_status import run_vpn_status
        res = run_vpn_status({}) or {}
        result = res.get("result") or {}
        healthy = bool(result.get("healthy"))
        probes = result.get("probes") or []
        detail = "; ".join(f"{p['label']}={p['detail']}" for p in probes)
    except Exception as e:
        logger.exception("monitor_vpn: vpn_status handler упал")
        healthy = False
        detail = f"handler crashed: {e.__class__.__name__}"

    down = bool(cache.get(_VPN_DOWN_KEY))

    if healthy:
        cache.set(_VPN_FAILS_KEY, 0, _STATE_TTL)
        if down:
            cache.set(_VPN_DOWN_KEY, False, _STATE_TTL)
            _alert(f"✅ VPN ВОССТАНОВЛЕН: туннель снова отвечает.\n{detail}")
        return

    fails = int(cache.get(_VPN_FAILS_KEY) or 0) + 1
    cache.set(_VPN_FAILS_KEY, fails, _STATE_TTL)
    logger.warning("VPN monitor: недоступен (%s), подряд %d/%d", detail, fails, threshold)
    if fails >= threshold and not down:
        cache.set(_VPN_DOWN_KEY, True, _STATE_TTL)
        now = timezone.localtime().strftime("%d.%m.%Y %H:%M:%S")
        _alert(
            f"🛑 VPN НЕДОСТУПЕН\n"
            f"Туннель awg0/claude0 не пропускает трафик до Telegram/Anthropic.\n"
            f"Userbot, leads-бот, sendMessage прода — не работают.\n\n"
            f"Проверка: {detail}\n"
            f"Неудач подряд: {fails}\n"
            f"Время: {now} (МСК)\n\n"
            f"Чек на хосте dev: `awg show awg0`, `systemctl status awg-quick@awg0`"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Daily report: сводный отчёт о работоспособности в 8:00, 13:00, 19:00 МСК.
# Включает /health/ обоих серверов, контейнеры, диск, миграции, VPN +
# бизнес-метрики (новые клиенты, консультации, сообщения по каналам, платежи).
# Расписание задаётся PeriodicTask (cron) — см. apps/core/migrations.
# ─────────────────────────────────────────────────────────────────────────────


def _local_status_report() -> tuple[bool, str, dict]:
    """Запускает status-handler локально (на этом сервере = dev) — возвращает
    (ok, краткое summary для отчёта, raw_result)."""
    try:
        from apps.devops.handlers.status import run_status
        out = run_status({}) or {}
        res = out.get("result") or {}
        return True, _summarize_status(res), res
    except Exception as e:
        return False, f"status: ошибка ({e.__class__.__name__})", {}


def _summarize_status(res: dict) -> str:
    """Из result status-handler'а собирает 4 коротких строки для daily-report."""
    git = res.get("git") or {}
    disk = res.get("disk") or {}
    migs = res.get("migrations") or {}
    running = res.get("containers_running")
    total = res.get("containers_total")
    cont_str = f"{running}/{total} up" if running is not None and total is not None else "?"
    pending = migs.get("pending", 0)
    mig_str = f"✅ 0 ждут" if pending == 0 else f"⚠ {pending} ждут"
    return (
        f"git: `{git.get('branch', '?')}` @ `{git.get('commit', '?')}`\n"
        f"контейнеры: {cont_str}\n"
        f"миграции: {mig_str}\n"
        f"диск: {disk.get('used_pct', '?')}%, свободно {disk.get('free_gb', '?')}G"
    )


def _prod_via_agent(action: str, timeout: int = 30) -> tuple[bool, str, dict]:
    """Дёргает прод-агента и возвращает (ok, output, result-dict)."""
    base = (getattr(settings, "PROD_AGENT_URL", "") or "").rstrip("/")
    token = (getattr(settings, "DEVOPS_AGENT_TOKEN_PROD", "") or "").strip()
    if not base or not token:
        return False, "PROD_AGENT_URL/DEVOPS_AGENT_TOKEN_PROD не заданы", {}
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.post(f"{base}/devops/agent/jobs/", headers=hdr,
                          json={"action_type": action, "params": {}}, timeout=20)
    except requests.RequestException as e:
        return False, f"прод недоступен ({e.__class__.__name__})", {}
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}", {}
    job_id = (r.json() or {}).get("id")
    if not job_id:
        return False, "агент не вернул id задачи", {}
    for _ in range(max(1, timeout // 2)):
        _time.sleep(2)
        try:
            jr = requests.get(f"{base}/devops/agent/jobs/{job_id}/", headers=hdr, timeout=15).json()
        except requests.RequestException:
            continue
        if jr.get("status") == "done":
            return True, jr.get("output") or "", jr.get("result") or {}
        if jr.get("status") == "failed":
            return False, jr.get("output") or "agent failed", {}
    return False, "таймаут ожидания агента", {}


def _format_business(stats: dict) -> str:
    """Бизнес-блок отчёта из daily_stats.result."""
    if not stats:
        return "(нет данных)"
    new = stats.get("new_clients", 0)
    cons = stats.get("consultations") or {}
    msgs = stats.get("messages") or {}
    by_chan = msgs.get("by_channel") or {}
    pays = stats.get("payments") or {}

    lines = [
        f"🆕 Новых клиентов: {new}",
        (
            f"📅 Консультации: ✅ {cons.get('done', 0)} проведено · "
            f"📌 {cons.get('booked', 0)} назначено · "
            f"↻ {cons.get('transferred', 0)} перенесено · "
            f"❌ {cons.get('cancelled', 0)} отменено"
        ),
        f"✉️ Сообщения: ↗{msgs.get('out', 0)} ↘{msgs.get('in', 0)}",
    ]
    for ch in sorted(by_chan):
        c = by_chan[ch]
        lines.append(f"   {ch}: ↗{c.get('out', 0)} ↘{c.get('in', 0)}")
    lines += [
        (
            f"💰 Платежи: ↗{pays.get('incoming_sum', 0):,.0f} ₽ "
            f"({pays.get('incoming_count', 0)} шт) · "
            f"↘{pays.get('outgoing_sum', 0):,.0f} ₽ "
            f"({pays.get('outgoing_count', 0)} шт)"
        ),
    ]
    return "\n".join(lines)


@shared_task(queue="devops")
def daily_health_report():
    """Собирает сводный отчёт по обоим серверам и шлёт в MAX+TG.

    Запускается по cron 08:00, 13:00, 19:00 МСК (PeriodicTask). Очередь `devops`
    нужна потому что _local_status_report() зовёт run_status handler, который
    лезет в docker.sock — этот сокет смонтирован только в devops-runner
    контейнер, не в обычный celery worker.

    Гейт MONITOR_BOT_POLL: на проде миграция тоже сидит, но без флага no-op.
    """
    if not getattr(settings, "MONITOR_BOT_POLL", False):
        return

    now = timezone.localtime().strftime("%d.%m.%Y %H:%M")

    # 1. dev — локально (мы и есть dev)
    dev_ok, dev_summary, _ = _local_status_report()

    # 2. prod — через DevOps-агент (он умеет даже когда web прода завис)
    prod_ok, _prod_out, prod_status_res = _prod_via_agent("status", timeout=45)
    prod_summary = _summarize_status(prod_status_res) if prod_ok else "❌ нет ответа от прод-агента"

    # 3. VPN — одна проверка с dev (см. monitor_vpn)
    try:
        from apps.devops.handlers.vpn_status import run_vpn_status
        vpn_res = (run_vpn_status({}) or {}).get("result") or {}
        vpn_line = "✅ туннель работает" if vpn_res.get("healthy") else "🛑 туннель недоступен"
        for p in vpn_res.get("probes") or []:
            mark = "✓" if p["ok"] else "✗"
            vpn_line += f"\n   {mark} {p['label']:<14s} {p['detail']}"
    except Exception as e:
        vpn_line = f"❌ ошибка проверки: {e.__class__.__name__}"

    # 4. бизнес-метрики — берём с прода (там живут CRM-данные)
    _, _, business_stats = _prod_via_agent("daily_stats", timeout=45)

    lines = [
        f"📋 Отчёт о работоспособности — {now} МСК",
        "",
        "🔵 DEV (crmsiri.ru)",
        *("   " + ln for ln in dev_summary.splitlines()),
        "",
        "🟢 PROD (siricrm.ru)",
        *("   " + ln for ln in prod_summary.splitlines()),
        "",
        f"🔒 VPN: {vpn_line}",
        "",
        "📊 Бизнес-метрики прода за сутки",
        _format_business(business_stats),
    ]
    text = "\n".join(lines)

    sent_max = _send_max_alert(text)
    sent_tg = _send_telegram_alert(text)
    logger.info("daily_health_report отправлен: MAX=%s TG=%s, len=%d", sent_max, sent_tg, len(text))


@shared_task
def poll_monitor_bot():
    if not getattr(settings, "MONITOR_BOT_POLL", False):
        return
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    allowed = _monitor_allowed_ids()
    if not token or not allowed:
        # Нет токена или некому отвечать (fail-closed) — не поллим.
        return
    # SETNX-лок: один long-poll за раз (иначе параллельные getUpdates → 409).
    if not cache.add(_MON_LOCK_KEY, "1", 28):
        return
    try:
        _poll_monitor_once(token, allowed)
    finally:
        cache.delete(_MON_LOCK_KEY)
