"""Status handler: собирает информацию об окружении.

Запускается в devops-runner контейнере, у которого:
- volume `.:/app` — доступ к репозиторию
- volume `/var/run/docker.sock` — управление контейнерами через Docker SDK
"""
import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout

import django
from django.core.management import call_command

from apps.devops.tasks import register_handler


REPO_DIR = "/app"


_GIT_ENV = {**os.environ, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": REPO_DIR}


def _run(cmd: list[str], cwd: str = REPO_DIR, timeout: int = 10,
         stdout_only: bool = False) -> str:
    """Выполнить команду и вернуть вывод (или короткое сообщение об ошибке).

    Для git-команд добавляем safe.directory, иначе в контейнере git ругается
    "dubious ownership" и пишет это в stdout/stderr.
    """
    env = _GIT_ENV if cmd and cmd[0] == "git" else None
    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            check=False, env=env,
        )
        if stdout_only:
            return (out.stdout or "").strip()
        return (out.stdout or out.stderr or "").strip()
    except FileNotFoundError:
        return f"<not installed: {cmd[0]}>"
    except subprocess.TimeoutExpired:
        return "<timeout>"
    except Exception as e:
        return f"<error: {e}>"


def _git_info() -> dict:
    info = {
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "commit": _run(["git", "rev-parse", "--short", "HEAD"]),
        "commit_message": _run(["git", "log", "-1", "--pretty=%s"]),
        "commit_date": _run(["git", "log", "-1", "--pretty=%ci"]),
        "dirty": bool(_run(["git", "status", "--porcelain"], stdout_only=True)),
        "ahead": 0,
        "behind": 0,
        "has_upstream": False,
    }
    # Насколько локальная ветка отстаёт/опережает upstream — сигнал «есть что деплоить».
    # git fetch здесь не делаем (долго) — показываем по последнему известному состоянию.
    counts = _run(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"],
                  stdout_only=True)
    parts = counts.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        info["ahead"], info["behind"] = int(parts[0]), int(parts[1])
        info["has_upstream"] = True
    return info


def _container_image_name(c) -> str:
    """Имя/тег образа контейнера БЕЗ обращения к Docker API.

    `c.image.tags` дёргает inspect образа и падает ImageNotFound, если образ
    стал dangling (только sha256, без тега) — типично после rebuild. Берём имя
    из атрибутов самого контейнера, которые уже загружены.
    """
    name = (c.attrs.get("Config", {}) or {}).get("Image") or ""
    if name and not name.startswith("sha256:"):
        return name
    img_id = c.attrs.get("Image", "") or ""
    return img_id[:19] if img_id else "<unknown>"


def _containers_info() -> list[dict]:
    """Список контейнеров compose-проекта через Docker SDK."""
    try:
        import docker
    except ImportError:
        return [{"error": "docker SDK not installed"}]

    try:
        client = docker.from_env()
        containers = client.containers.list(all=True)
    except Exception as e:
        return [{"error": str(e)}]

    result = []
    for c in containers:
        # Берём только наши контейнеры (имя начинается с siricrm-)
        if not c.name.startswith("siricrm"):
            continue
        try:
            state = c.attrs.get("State", {}) or {}
            result.append({
                "name": c.name,
                "status": c.status,
                "health": (state.get("Health", {}) or {}).get("Status", ""),
                "image": _container_image_name(c),
                "created": (c.attrs.get("Created", "") or "")[:19],
                "started_at": (state.get("StartedAt", "") or "")[:19],
                "restarts": c.attrs.get("RestartCount", 0),
            })
        except Exception as e:  # один кривой контейнер не должен валить весь список
            result.append({"name": getattr(c, "name", "?"), "status": "?", "error": str(e)})
    result.sort(key=lambda x: x["name"])
    return result


def _migrations_info() -> dict:
    """Подсчёт примененных и неприменённых миграций."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            call_command("showmigrations", "--plan", verbosity=1, no_color=True)
    except Exception as e:
        return {"error": str(e)}

    applied = pending = 0
    pending_list = []
    for line in buf.getvalue().splitlines():
        s = line.strip()
        if s.startswith("[X]"):
            applied += 1
        elif s.startswith("[ ]"):
            pending += 1
            pending_list.append(s[3:].strip())
    return {"applied": applied, "pending": pending, "pending_list": pending_list[:10]}


def _disk_info() -> dict:
    """df -h /"""
    total, used, free = shutil.disk_usage("/")
    return {
        "total_gb": round(total / 2**30, 1),
        "used_gb": round(used / 2**30, 1),
        "free_gb": round(free / 2**30, 1),
        "used_pct": round(used / total * 100),
    }


def _system_mem() -> dict:
    """Память хоста из /proc/meminfo (kB → GB)."""
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                info[k.strip()] = int(rest.split()[0])  # kB
        total = info.get("MemTotal", 0) / 2**20
        avail = info.get("MemAvailable", 0) / 2**20
        used = total - avail
        return {
            "total_gb": round(total, 1), "used_gb": round(used, 1),
            "avail_gb": round(avail, 1),
            "used_pct": round(used / total * 100) if total else 0,
        }
    except Exception as e:
        return {"error": str(e)}


def _docker_stats() -> dict:
    """Снимок CPU/RAM по контейнерам через `docker stats --no-stream`.
    Возвращает {name: {cpu, mem}}; пусто при ошибке/таймауте."""
    out = _run(
        ["docker", "stats", "--no-stream", "--format",
         "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"],
        timeout=20, stdout_only=True,
    )
    stats = {}
    if not out or out.startswith("<"):
        return stats
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0].startswith("siricrm"):
            stats[parts[0]] = {"cpu": parts[1].strip(),
                               "mem": parts[2].strip(), "mem_pct": parts[3].strip()}
    return stats


def _versions() -> dict:
    return {
        "python": sys.version.split()[0],
        "django": django.get_version(),
        "env": os.environ.get("DJANGO_ENV", "dev"),
    }


@register_handler("status")
def run_status(params: dict) -> dict:
    git = _git_info()
    containers = _containers_info()
    migrations = _migrations_info()
    disk = _disk_info()
    mem = _system_mem()
    cstats = _docker_stats()
    versions = _versions()

    sync = ""
    if git.get("has_upstream"):
        bits = []
        if git["behind"]:
            bits.append(f"отстаёт на {git['behind']} (есть что деплоить)")
        if git["ahead"]:
            bits.append(f"опережает на {git['ahead']}")
        sync = (" — " + ", ".join(bits)) if bits else " — в синхроне с remote"

    output_lines = [
        f"Git: {git['branch']} @ {git['commit']} — {git['commit_message']}{sync}",
        f"     {git['commit_date']}" + (" (есть незакоммиченные изменения!)" if git["dirty"] else ""),
        "",
        f"Контейнеры: {len(containers)}",
    ]
    for c in containers:
        if "error" in c:
            output_lines.append(f"  {c.get('name', '?'):<25} ERROR: {c['error']}")
        else:
            extra = f" [{c['health']}]" if c.get("health") else ""
            extra += f" ⟳{c['restarts']}" if c.get("restarts") else ""
            st = cstats.get(c["name"])
            if st:
                extra += f"  cpu {st['cpu']}, mem {st['mem']}"
            output_lines.append(f"  {c['name']:<25} {c['status']}{extra}")
    mem_line = (
        f"Память:   {mem['used_gb']}G / {mem['total_gb']}G ({mem['used_pct']}%)"
        if "error" not in mem else f"Память:   <{mem['error']}>"
    )
    output_lines.extend([
        "",
        f"Миграции: применено={migrations.get('applied', '?')} ждут={migrations.get('pending', '?')}",
        f"Диск:     {disk['used_gb']}G / {disk['total_gb']}G ({disk['used_pct']}%)",
        mem_line,
        f"Версии:   Python {versions['python']}, Django {versions['django']}, env={versions['env']}",
    ])

    total = len(containers)
    running = sum(1 for c in containers if c.get("status") == "running" and "error" not in c)
    return {
        "output": "\n".join(output_lines),
        "result": {
            "git": git,
            "containers": containers,
            "containers_total": total,
            "containers_running": running,
            "containers_ok": (total > 0 and running == total),
            "migrations": migrations,
            "disk": disk,
            "mem": mem,
            "container_stats": cstats,
            "versions": versions,
        },
    }
