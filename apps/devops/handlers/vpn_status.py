"""vpn_status handler: проверка работоспособности WireGuard/AmneziaWG-туннеля.

🛑 Бизнес-логика «работоспособность» = «исходящее HTTPS до подсетей, которые
у нас в AllowedIPs туннеля, идёт за разумный таймаут». В этих CIDR живут
Telegram (api.telegram.org) и Anthropic (api.anthropic.com), маршрут до них
ведёт через интерфейс VPN — если HTTP-запрос проходит, значит туннель
поднят, peer отвечает, exit-нода не запретила трафик.

Запускается в devops-runner: запрос идёт из контейнера → docker bridge →
хост-routing → интерфейс VPN (awg0 на dev / claude0 на prod). Привилегий
на работу с самим интерфейсом не требуется — это эффективнее, чем парсить
`wg show` через subprocess (контейнер не имеет CAP_NET_ADMIN).

Используется:
- кнопкой бота (опционально, потом)
- celery-таской monitor_vpn (каждую минуту) для алёртов
- daily_health_report (3 раза в сутки) — поле «VPN» в отчёте
"""
import time

import requests

from apps.devops.tasks import register_handler


# Что проверяем (URL → label для отчёта). Оба endpoint'а — внутри AllowedIPs
# VPN-туннеля, маршрут к ним идёт через интерфейс VPN. Один может временно
# таймаутить (DDoS-фильтр, ratelimit) — но оба разом = точно VPN/туннель.
PROBES = (
    ("https://api.telegram.org/", "Telegram API"),
    ("https://api.anthropic.com/", "Anthropic API"),
)
PROBE_TIMEOUT = 6  # секунд каждый


def _probe(url: str) -> tuple[bool, str]:
    """True если HTTP-ответ получен (любой код 2xx-4xx — значит туннель жив,
    содержательное состояние API нам тут не важно). Возвращает (ok, detail)."""
    t0 = time.monotonic()
    try:
        r = requests.get(url, timeout=PROBE_TIMEOUT, allow_redirects=False)
        dt_ms = int((time.monotonic() - t0) * 1000)
        # 2xx-4xx — туннель работает, сервер ответил. 5xx — туннель работает,
        # сервер ответил с ошибкой (не наша проблема). Только сетевое падение = VPN-проблема.
        return True, f"HTTP {r.status_code} ({dt_ms} мс)"
    except requests.exceptions.ConnectTimeout:
        return False, f"connect timeout >{PROBE_TIMEOUT}с"
    except requests.exceptions.ReadTimeout:
        return False, f"read timeout >{PROBE_TIMEOUT}с"
    except requests.exceptions.SSLError as e:
        return False, f"SSL {str(e)[:80]}"
    except requests.exceptions.ConnectionError as e:
        msg = str(e)
        if "Temporary failure in name resolution" in msg or "Name or service not known" in msg:
            return False, "DNS не резолвит (см. dns-fallback-prod)"
        return False, f"connection: {msg[:100]}"
    except Exception as e:
        return False, f"{e.__class__.__name__}: {str(e)[:80]}"


@register_handler("vpn_status")
def run_vpn_status(params: dict) -> dict:
    results = []
    for url, label in PROBES:
        ok, detail = _probe(url)
        results.append({"label": label, "url": url, "ok": ok, "detail": detail})

    healthy_count = sum(1 for r in results if r["ok"])
    # Считаем VPN живым если ОБА endpoint'а ответили. Один может временно глючить
    # (например, Telegram блокирует SNI), но оба — точно туннель/AllowedIPs/peer.
    healthy = healthy_count == len(results)

    lines = ["=== VPN status ==="]
    for r in results:
        mark = "✓" if r["ok"] else "✗"
        lines.append(f"  {mark} {r['label']:<14s} {r['detail']}")
    lines.append("")
    lines.append("VPN: OK" if healthy else "VPN: НЕДОСТУПЕН")

    return {
        "output": "\n".join(lines),
        "result": {
            "healthy": healthy,
            "probes": results,
            "healthy_count": healthy_count,
            "total_count": len(results),
        },
    }
