"""Disk usage handler: разбивка по тому, что занимает диск на сервере.

Запускается в devops-runner. Видит:
- свой корневой ФС (тот же overlay2-backing что и host /, поэтому df / даёт
  релевантную картину)
- volume-смонтированные директории репозитория (/app/{backups,logs,...})
- docker.sock — даёт `docker system df` + размеры volumes
"""
import os
import shutil
import subprocess
from pathlib import Path

from apps.devops.tasks import register_handler

REPO_DIR = "/app"


def _du_bytes(path: str) -> int | None:
    """`du -sb path` — размер директории в байтах. None если путь не существует."""
    if not os.path.exists(path):
        return None
    try:
        out = subprocess.run(
            ["du", "-sb", path], capture_output=True, text=True, timeout=60, check=False,
        )
        if out.returncode != 0:
            return None
        return int(out.stdout.split()[0])
    except Exception:
        return None


def _human(n: int | None) -> str:
    if n is None:
        return "n/a"
    if n < 1024:
        return f"{n}B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f}{unit}"
    return f"{n:.1f}PB"


def _df_root() -> dict:
    total, used, free = shutil.disk_usage("/")
    return {
        "total_gb": round(total / 2**30, 1),
        "used_gb": round(used / 2**30, 1),
        "free_gb": round(free / 2**30, 1),
        "used_pct": round(used / total * 100, 1) if total else 0,
    }


def _docker_system_df() -> dict | None:
    """`docker system df` через SDK: сколько занимают images / containers / volumes / build cache."""
    try:
        import docker
        client = docker.from_env()
        # API endpoint: GET /system/df
        data = client.df()
        return {
            "images": {
                "count": len(data.get("Images") or []),
                "total_bytes": sum((i.get("Size") or 0) for i in (data.get("Images") or [])),
                "reclaimable_bytes": sum((i.get("Size") or 0) - (i.get("SharedSize") or 0)
                                          for i in (data.get("Images") or [])
                                          if (i.get("Containers") or 0) == 0),
            },
            "containers": {
                "count": len(data.get("Containers") or []),
                "total_bytes": sum((c.get("SizeRw") or 0) for c in (data.get("Containers") or [])),
            },
            "volumes": [
                {
                    "name": v.get("Name"),
                    "size_bytes": (v.get("UsageData") or {}).get("Size", -1),
                    "refs": (v.get("UsageData") or {}).get("RefCount", -1),
                }
                for v in (data.get("Volumes") or [])
            ],
            "build_cache_bytes": sum((b.get("Size") or 0) for b in (data.get("BuildCache") or [])),
        }
    except Exception as e:
        return {"error": str(e)}


@register_handler("disk_usage")
def run_disk_usage(params: dict) -> dict:
    log: list[str] = []

    # 1. Общий df /
    df = _df_root()
    log.append(f"=== Диск контейнера (= host /) ===")
    log.append(f"Total: {df['total_gb']}G, used: {df['used_gb']}G ({df['used_pct']}%), free: {df['free_gb']}G")

    # 2. Размеры volume-смонтированных директорий репо
    log.append("\n=== Каталоги репозитория (через volume) ===")
    repo_dirs = {}
    for name in ("backups", "logs", "media", "static", "staticfiles", "node_modules"):
        path = os.path.join(REPO_DIR, name)
        size = _du_bytes(path)
        repo_dirs[name] = size
        log.append(f"  /app/{name:14s} {_human(size)}")
    repo_total = _du_bytes(REPO_DIR)
    repo_dirs["__total__"] = repo_total
    log.append(f"  /app (всего)    {_human(repo_total)}")

    # 3. Docker system df через SDK
    log.append("\n=== Docker (через docker.sock) ===")
    dsdf = _docker_system_df()
    if dsdf and "error" not in dsdf:
        images = dsdf["images"]
        containers = dsdf["containers"]
        log.append(f"  images:    {images['count']} шт, занимают {_human(images['total_bytes'])}, "
                   f"можно освободить {_human(images['reclaimable_bytes'])}")
        log.append(f"  containers (writable): {_human(containers['total_bytes'])} writable layer на {containers['count']} контейнерах")
        log.append(f"  build cache:           {_human(dsdf['build_cache_bytes'])}")
        log.append(f"  volumes ({len(dsdf['volumes'])} шт):")
        # Сортируем volume'ы по размеру (-1 = unknown в конец)
        vols = sorted(dsdf["volumes"], key=lambda v: v["size_bytes"] if v["size_bytes"] >= 0 else 0, reverse=True)
        for v in vols[:20]:
            size_str = _human(v["size_bytes"]) if v["size_bytes"] >= 0 else "n/a (Docker не считает)"
            log.append(f"    {v['name'][:55]:55s} {size_str:>12s}  refs={v['refs']}")
    else:
        log.append(f"  ошибка: {(dsdf or {}).get('error', 'unknown')}")

    return {
        "output": "\n".join(log),
        "result": {
            "df": df,
            "repo_dirs": repo_dirs,
            "docker": dsdf,
        },
    }
