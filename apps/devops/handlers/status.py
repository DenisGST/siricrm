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


def _run(cmd: list[str], cwd: str = REPO_DIR, timeout: int = 10) -> str:
    """Выполнить команду и вернуть stdout (или короткое сообщение об ошибке)."""
    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return (out.stdout or out.stderr or "").strip()
    except FileNotFoundError:
        return f"<not installed: {cmd[0]}>"
    except subprocess.TimeoutExpired:
        return "<timeout>"
    except Exception as e:
        return f"<error: {e}>"


def _git_info() -> dict:
    return {
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "commit": _run(["git", "rev-parse", "--short", "HEAD"]),
        "commit_message": _run(["git", "log", "-1", "--pretty=%s"]),
        "commit_date": _run(["git", "log", "-1", "--pretty=%ci"]),
        "dirty": bool(_run(["git", "status", "--porcelain"])),
    }


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
        result.append({
            "name": c.name,
            "status": c.status,
            "image": (c.image.tags[0] if c.image.tags else c.image.short_id),
            "created": c.attrs.get("Created", "")[:19],
            "started_at": (c.attrs.get("State", {}).get("StartedAt", "") or "")[:19],
        })
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
    versions = _versions()

    output_lines = [
        f"Git: {git['branch']} @ {git['commit']} — {git['commit_message']}",
        f"     {git['commit_date']}" + (" (dirty!)" if git["dirty"] else ""),
        "",
        f"Containers: {len(containers)}",
    ]
    for c in containers:
        if "error" in c:
            output_lines.append(f"  ERROR: {c['error']}")
        else:
            output_lines.append(f"  {c['name']:<25} {c['status']}")
    output_lines.extend([
        "",
        f"Migrations: applied={migrations.get('applied', '?')} pending={migrations.get('pending', '?')}",
        f"Disk:       {disk['used_gb']}G / {disk['total_gb']}G ({disk['used_pct']}%)",
        f"Versions:   Python {versions['python']}, Django {versions['django']}, env={versions['env']}",
    ])

    return {
        "output": "\n".join(output_lines),
        "result": {
            "git": git,
            "containers": containers,
            "migrations": migrations,
            "disk": disk,
            "versions": versions,
        },
    }
