"""Deploy handler: git pull → migrate → restart web/celery контейнеров.

Запускается в devops-runner. Сам себя НЕ перезапускает — следующий deploy
подхватит новый код runner-а.

Полный rebuild image (для requirements.txt и Dockerfile изменений) — отдельной
кнопкой/handler-ом в будущем; пока работает только если код берётся через volume
.:/app (что и есть в нашем docker-compose.prod.yml).
"""
import os
import subprocess
import time

import requests

from apps.devops.tasks import register_handler


REPO_DIR = "/app"
RESTART_CONTAINERS = ["siricrm-web-1", "siricrm-celery-1", "siricrm-celery-beat-1"]
HEALTH_URL = "http://web:8000/health/"


def _run(cmd: list[str], cwd: str = REPO_DIR, timeout: int = 60) -> tuple[int, str]:
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                         timeout=timeout, check=False)
    body = (out.stdout or "") + (("\n" + out.stderr) if out.stderr else "")
    return out.returncode, body.strip()


def _docker_client():
    import docker
    return docker.from_env()


@register_handler("deploy")
def run_deploy(params: dict) -> dict:
    branch = params.get("branch")  # если не задано — текущая ветка
    skip_migrate = bool(params.get("skip_migrate", False))
    skip_restart = bool(params.get("skip_restart", False))

    log: list[str] = []
    result: dict = {}

    # 1. Git: fetch + (checkout) + pull
    log.append("=== Git ===")
    code, out = _run(["git", "fetch", "origin"])
    log.append(f"git fetch: rc={code}\n{out}")
    if code != 0:
        raise RuntimeError("git fetch failed")

    if branch:
        code, out = _run(["git", "checkout", branch])
        log.append(f"git checkout {branch}: rc={code}\n{out}")
        if code != 0:
            raise RuntimeError(f"git checkout {branch} failed")

    code, out = _run(["git", "pull", "--ff-only"])
    log.append(f"git pull --ff-only: rc={code}\n{out}")
    if code != 0:
        raise RuntimeError("git pull failed (non fast-forward?)")

    code, head = _run(["git", "log", "-1", "--pretty=%h %s"])
    log.append(f"HEAD: {head}")
    result["head"] = head

    # 2. Migrate (через docker exec в контейнере web — он видит свежий код через volume)
    if not skip_migrate:
        log.append("\n=== Migrate ===")
        client = _docker_client()
        try:
            web = client.containers.get("siricrm-web-1")
        except Exception as e:
            raise RuntimeError(f"web container not found: {e}")
        rc, out_bytes = web.exec_run(
            cmd=["python", "manage.py", "migrate", "--noinput"],
            workdir="/app",
        )
        out_str = out_bytes.decode("utf-8", errors="replace")[-2000:]
        log.append(f"migrate: rc={rc}\n{out_str}")
        if rc != 0:
            raise RuntimeError(f"migrate failed (rc={rc})")
        result["migrate_rc"] = rc

    # 3. Restart Python-контейнеров (web, celery, celery-beat)
    if not skip_restart:
        log.append("\n=== Restart ===")
        client = _docker_client()
        restarted = []
        for name in RESTART_CONTAINERS:
            try:
                c = client.containers.get(name)
                c.restart(timeout=20)
                restarted.append(name)
                log.append(f"  {name}: restarted")
            except Exception as e:
                log.append(f"  {name}: ERROR {e}")
        result["restarted"] = restarted

        # 4. Healthcheck (после restart)
        log.append("\n=== Healthcheck ===")
        time.sleep(8)
        for attempt in range(1, 6):
            try:
                r = requests.get(HEALTH_URL, timeout=5)
                log.append(f"  attempt {attempt}: HTTP {r.status_code} → {r.text[:200]}")
                if r.status_code == 200:
                    result["healthcheck"] = "ok"
                    break
            except Exception as e:
                log.append(f"  attempt {attempt}: {e}")
            time.sleep(3)
        else:
            log.append("  Healthcheck не прошёл — проверь логи web!")
            result["healthcheck"] = "failed"

    return {"output": "\n".join(log), "result": result}
