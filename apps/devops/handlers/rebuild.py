"""Rebuild handler: git pull → docker compose build → up -d (пересборка образов).

В отличие от deploy (который только перезапускает контейнеры со старым образом),
rebuild ПЕРЕСОБИРАЕТ Docker-образы — нужно при изменениях Dockerfile / requirements.txt.

Требует: docker CLI + compose plugin в образе (см. Dockerfile) и монтирование
репозитория по ХОСТ-ПУТИ (HOST_REPO_DIR), чтобы пути в compose.yml резолвились
корректно при запуске из контейнера через docker.sock.

Сам devops-runner НЕ пересоздаётся (исключён из up) — обновится при следующем rebuild.
"""
import os
import subprocess
import time

import requests

from apps.devops.tasks import register_handler


# Эти переменные задаются в .env.{prod,dev}:
HOST_REPO_DIR = os.environ.get("HOST_REPO_DIR", "/app")
COMPOSE_FILE = os.environ.get("COMPOSE_FILE_NAME", "docker-compose.prod-host.yml")
ENV_FILE = os.environ.get("ENV_FILE", ".env.prod")

HEALTH_URL = "http://web:8000/health/"
EXCLUDE_FROM_UP = {"devops-runner", "db", "redis"}

_GIT_ENV = {**os.environ, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": HOST_REPO_DIR}


def _run(cmd: list[str], timeout: int = 600, env: dict | None = None) -> tuple[int, str]:
    out = subprocess.run(cmd, cwd=HOST_REPO_DIR, capture_output=True, text=True,
                         timeout=timeout, check=False, env=env)
    body = (out.stdout or "") + (("\n" + out.stderr) if out.stderr else "")
    return out.returncode, body.strip()


def _compose(*args: str, timeout: int = 600) -> tuple[int, str]:
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, "--env-file", ENV_FILE, *args]
    env = {**os.environ, "ENV_FILE": ENV_FILE}
    return _run(cmd, timeout=timeout, env=env)


@register_handler("rebuild")
def run_rebuild(params: dict) -> dict:
    log: list[str] = []
    result: dict = {}

    if HOST_REPO_DIR == "/app":
        raise RuntimeError(
            "HOST_REPO_DIR не задан в окружении devops-runner — rebuild невозможен. "
            "Добавь HOST_REPO_DIR в .env и пересоздай devops-runner."
        )

    # 1. git pull
    log.append("=== Git ===")
    rc, out = _run(["git", "fetch", "origin"], env=_GIT_ENV)
    log.append(f"git fetch: rc={rc}\n{out}")
    branch = params.get("branch")
    if branch:
        rc, out = _run(["git", "checkout", branch], env=_GIT_ENV)
        log.append(f"git checkout {branch}: rc={rc}\n{out}")
        if rc != 0:
            raise RuntimeError(f"git checkout {branch} failed")
    rc, out = _run(["git", "pull", "--ff-only"], env=_GIT_ENV)
    log.append(f"git pull: rc={rc}\n{out}")
    if rc != 0:
        raise RuntimeError("git pull failed")
    rc, head = _run(["git", "log", "-1", "--pretty=%h %s"], env=_GIT_ENV)
    log.append(f"HEAD: {head}")
    result["head"] = head

    # 2. docker compose build
    log.append("\n=== Build ===")
    rc, out = _compose("build", timeout=900)
    log.append(f"compose build: rc={rc}\n{out[-3000:]}")
    if rc != 0:
        raise RuntimeError("docker compose build failed")

    # 3. Список сервисов → исключаем devops-runner/db/redis → up -d
    rc, services_out = _compose("config", "--services", timeout=30)
    services = [s.strip() for s in services_out.splitlines() if s.strip()]
    to_up = [s for s in services if s not in EXCLUDE_FROM_UP]
    log.append(f"\n=== Up (recreate): {', '.join(to_up)} ===")
    rc, out = _compose("up", "-d", "--no-deps", *to_up, timeout=300)
    log.append(f"compose up: rc={rc}\n{out[-2000:]}")
    if rc != 0:
        raise RuntimeError("docker compose up failed")
    result["recreated"] = to_up

    # 4. Healthcheck
    log.append("\n=== Healthcheck ===")
    host_header = (os.environ.get("ALLOWED_HOSTS", "") or "localhost").split(",")[0].strip()
    time.sleep(10)
    for attempt in range(1, 7):
        try:
            r = requests.get(HEALTH_URL, headers={"Host": host_header}, timeout=5)
            log.append(f"  attempt {attempt}: HTTP {r.status_code} → {r.text[:200]}")
            if r.status_code == 200:
                result["healthcheck"] = "ok"
                break
        except Exception as e:
            log.append(f"  attempt {attempt}: {e}")
        time.sleep(4)
    else:
        log.append("  Healthcheck не прошёл — проверь логи web!")
        result["healthcheck"] = "failed"

    return {"output": "\n".join(log), "result": result}
