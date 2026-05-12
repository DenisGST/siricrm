"""git_log handler: список последних коммитов текущей ветки (для выбора точки отката).

Запускается в devops-runner (volume `.:/app`). Только читает git, ничего не меняет.
"""
import os
import subprocess

from apps.devops.tasks import register_handler

REPO_DIR = "/app"
_GIT_ENV = {**os.environ, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": REPO_DIR}

# Разделитель полей, который точно не встретится в данных.
_SEP = "\x1f"
_FMT = _SEP.join(["%h", "%H", "%s", "%an", "%ci", "%D"])


def _git(*args: str, timeout: int = 15) -> tuple[int, str]:
    out = subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True,
                         text=True, timeout=timeout, check=False, env=_GIT_ENV)
    return out.returncode, (out.stdout or "").rstrip("\n")


@register_handler("git_log")
def run_git_log(params: dict) -> dict:
    limit = int(params.get("limit") or 30)
    limit = max(1, min(limit, 100))

    rc, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch.strip() if rc == 0 else "?"
    rc, head_full = _git("rev-parse", "HEAD")
    head_full = head_full.strip() if rc == 0 else ""

    # Подтянем удалённые refs, чтобы видеть, сколько коммитов «впереди».
    _git("fetch", "origin", timeout=20)

    rc, raw = _git("log", f"-{limit}", f"--pretty=format:{_FMT}", "HEAD")
    commits: list[dict] = []
    if rc == 0 and raw:
        for line in raw.splitlines():
            parts = line.split(_SEP)
            if len(parts) < 6:
                continue
            sha, full, msg, author, date, refs = parts[:6]
            commits.append({
                "sha": sha,
                "full_sha": full,
                "message": msg,
                "author": author,
                "date": (date or "")[:19],
                "refs": refs.strip(),
                "is_head": (full == head_full),
            })

    output_lines = [f"Ветка: {branch}", ""]
    for c in commits:
        mark = " ◀ ТЕКУЩАЯ" if c["is_head"] else ""
        refs = f"  ({c['refs']})" if c["refs"] else ""
        output_lines.append(f"  {c['sha']}  {c['date']}  {c['author']:<16} {c['message']}{refs}{mark}")

    return {
        "output": "\n".join(output_lines),
        "result": {"branch": branch, "head": head_full[:7], "commits": commits},
    }
