"""Rollback handler: откат кода (и, по возможности, миграций) на указанный коммит.

Запускается так же, как deploy (через агента целевого окружения → его devops-runner).

Логика:
  1. Запоминаем текущий HEAD.
  2. Считаем, какие миграции «потеряются» при откате (есть в HEAD, нет в target).
  3. Если такие есть — пытаемся реверснуть их в БД ДО смены кода (пока файлы на месте).
     Если хоть одна необратима / migrate упал — ОТКАТ ПРЕРЫВАЕТСЯ (код не трогаем),
     пользователю предлагается откатиться вручную / восстановить БД из бэкапа.
  4. git reset --hard <target>.
  5. restart web/celery/celery-beat → healthcheck.
"""
import os
import re
import subprocess
import time

import requests

from apps.devops.tasks import register_handler

REPO_DIR = "/app"
RESTART_CONTAINERS = ["siricrm-web-1", "siricrm-celery-1", "siricrm-celery-beat-1"]
HEALTH_URL = "http://web:8000/health/"
_GIT_ENV = {**os.environ, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": REPO_DIR}
_MIGRATION_RE = re.compile(r"^\d{4}.*\.py$")


def _run(cmd: list[str], timeout: int = 60, git: bool = False) -> tuple[int, str]:
    out = subprocess.run(cmd, cwd=REPO_DIR, capture_output=True, text=True,
                         timeout=timeout, check=False, env=_GIT_ENV if git else None)
    body = (out.stdout or "") + (("\n" + out.stderr) if out.stderr else "")
    return out.returncode, body.strip()


def _docker_client():
    import docker
    return docker.from_env()


def _web_exec(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    client = _docker_client()
    web = client.containers.get("siricrm-web-1")
    rc, out = web.exec_run(cmd=cmd, workdir="/app")
    return rc, out.decode("utf-8", errors="replace")


def _app_label_from_path(path: str) -> str | None:
    parts = path.split("/")
    if "migrations" in parts:
        i = parts.index("migrations")
        if i >= 1:
            return parts[i - 1]
    return None


def _lost_migrations(target: str, head: str) -> dict[str, list[str]]:
    """{app_label: [migration_name, ...]} — миграции, добавленные между target и head."""
    rc, raw = _run(["git", "diff", "--diff-filter=A", "--name-only",
                    f"{target}..{head}", "--", "*/migrations/*.py"], git=True)
    result: dict[str, list[str]] = {}
    if rc != 0:
        return result
    for path in raw.splitlines():
        path = path.strip()
        base = path.rsplit("/", 1)[-1]
        if not _MIGRATION_RE.match(base):
            continue
        app = _app_label_from_path(path)
        if not app:
            continue
        result.setdefault(app, []).append(base[:-3])  # без .py
    return result


def _target_migration(app: str, target_commit: str) -> str:
    """Имя миграции, до которой нужно откатить app (последняя, существующая в target),
    либо 'zero' если в target миграций нет."""
    rc, raw = _run(["git", "ls-tree", "-r", "--name-only", target_commit,
                    "--", f"apps/{app}/migrations/"], git=True)
    names = []
    if rc == 0:
        for path in raw.splitlines():
            base = path.strip().rsplit("/", 1)[-1]
            if _MIGRATION_RE.match(base):
                names.append(base[:-3])
    return max(names) if names else "zero"


@register_handler("rollback")
def run_rollback(params: dict) -> dict:
    target = (params.get("target_commit") or "").strip()
    if not target:
        raise ValueError("target_commit обязателен")
    skip_migrate = bool(params.get("skip_migrate_reverse"))

    log: list[str] = []
    result: dict = {}

    # 1. Текущее состояние
    rc, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], git=True)
    branch = branch.strip()
    rc, from_head_full = _run(["git", "rev-parse", "HEAD"], git=True)
    from_head_full = from_head_full.strip()
    rc, from_head = _run(["git", "log", "-1", "--pretty=%h %s", "HEAD"], git=True)
    result["from_head"] = from_head
    log.append(f"Ветка: {branch}")
    log.append(f"Сейчас: {from_head}")

    # Проверяем, что target существует и является коммитом
    rc, _ = _run(["git", "cat-file", "-e", f"{target}^{{commit}}"], git=True)
    if rc != 0:
        raise RuntimeError(f"Коммит {target} не найден в репозитории")
    rc, target_desc = _run(["git", "log", "-1", "--pretty=%h %s", target], git=True)
    log.append(f"Откат на: {target_desc}")

    rc, same = _run(["git", "rev-parse", target], git=True)
    if same.strip() == from_head_full:
        log.append("Уже на этом коммите — нечего откатывать.")
        return {"output": "\n".join(log), "result": result}

    # git reset --hard молча затирает незакоммиченные изменения — не делаем этого вслепую.
    rc, dirty = _run(["git", "status", "--porcelain"], git=True)
    if dirty.strip():
        raise RuntimeError(
            "Рабочее дерево не чисто (есть незакоммиченные изменения) — откат отменён, "
            f"чтобы их не потерять:\n{dirty.strip()[:1000]}"
        )

    # 2. Какие миграции потеряются
    lost = _lost_migrations(target, from_head_full)
    if lost and not skip_migrate:
        log.append("\n=== Миграции, которые будут реверснуты ===")
        plan = {}
        for app, names in sorted(lost.items()):
            tgt = _target_migration(app, target)
            plan[app] = tgt
            log.append(f"  {app}: {', '.join(sorted(names))}  →  migrate {app} {tgt}")

        # 3. Реверс ДО смены кода. Любая ошибка → прерываем откат.
        log.append("\n=== Реверс миграций ===")
        rolled_back = []
        for app, tgt in plan.items():
            rc, out = _web_exec(["python", "manage.py", "migrate", app, tgt, "--noinput"])
            log.append(f"  migrate {app} {tgt}: rc={rc}\n{out.strip()[-1500:]}")
            if rc != 0:
                log.append(
                    "\n⛔ ОТКАТ ПРЕРВАН: не удалось реверснуть миграции приложения "
                    f"'{app}' (вероятно, миграция необратима). Код НЕ изменён.\n"
                    "Варианты: 1) откатить вручную и восстановить БД из бэкапа (pull_db); "
                    "2) выбрать более позднюю точку отката (без проблемной миграции); "
                    "3) повторить откат с флагом skip_migrate_reverse (БД останется как есть)."
                )
                result["aborted"] = True
                result["migrations_rolled_back"] = rolled_back
                result["migrations_warning"] = f"откат прерван на миграциях приложения {app}"
                return {"output": "\n".join(log), "result": result}
            rolled_back.append(f"{app}→{tgt}")
        result["migrations_rolled_back"] = rolled_back
    elif lost and skip_migrate:
        log.append("\n⚠ Пропускаю реверс миграций (skip_migrate_reverse). В django_migrations "
                   "останутся записи без файлов — БД может быть несогласована с кодом.")
        result["migrations_warning"] = "реверс миграций пропущен — БД может быть несогласована"
    else:
        log.append("\nИзменений миграций между версиями нет — реверс не нужен.")

    # 4. Сам откат кода
    log.append("\n=== git reset --hard ===")
    rc, out = _run(["git", "reset", "--hard", target], git=True)
    log.append(f"rc={rc}\n{out}")
    if rc != 0:
        raise RuntimeError("git reset --hard не удался")
    rc, new_head = _run(["git", "log", "-1", "--pretty=%h %s"], git=True)
    log.append(f"HEAD теперь: {new_head}")
    result["head"] = new_head

    # 5. Restart + healthcheck
    log.append("\n=== Restart ===")
    client = _docker_client()
    restarted = []
    for name in RESTART_CONTAINERS:
        try:
            client.containers.get(name).restart(timeout=20)
            restarted.append(name)
            log.append(f"  {name}: restarted")
        except Exception as e:
            log.append(f"  {name}: ERROR {e}")
    result["restarted"] = restarted

    log.append("\n=== Healthcheck ===")
    host_header = (os.environ.get("ALLOWED_HOSTS", "") or "localhost").split(",")[0].strip()
    time.sleep(8)
    for attempt in range(1, 6):
        try:
            r = requests.get(HEALTH_URL, headers={"Host": host_header}, timeout=5)
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
