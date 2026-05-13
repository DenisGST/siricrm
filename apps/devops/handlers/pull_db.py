"""Pull DB: запросить backup у source_env, скачать, restore на ЭТОМ сервере.

Этот handler ВСЕГДА выполняется на dev-стороне (целевом). Запрашивает backup
у source environment через AgentClient, дожидается, скачивает с S3,
дропает текущую схему и применяет дамп.

ОПАСНО: полностью перезаписывает БД на этом сервере!
"""
import gzip
import os
import time
from io import BytesIO
from pathlib import Path

import boto3
import requests
from botocore.client import Config
from django.utils import timezone

from apps.devops.agent_client import AgentClient
from apps.devops.models import Environment
from apps.devops.tasks import register_handler


BACKUP_DIR = Path("/app/backups")


def _docker_client():
    import docker
    return docker.from_env()


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_S3_BASE_URL"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_S3_REGION_NAME", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            s3={"payload_signing_enabled": False, "addressing_style": "path"},
        ),
    )


def _wait_for_remote_job(client: AgentClient, remote_job_id: str,
                        log: list[str], timeout_sec: int = 600) -> dict:
    started = time.monotonic()
    while True:
        if time.monotonic() - started > timeout_sec:
            raise TimeoutError(f"Remote job {remote_job_id} not finished in {timeout_sec}s")
        job = client.get_job(remote_job_id)
        st = job.get("status")
        if st == "done":
            return job
        if st == "failed":
            raise RuntimeError(f"Remote job failed: {job.get('output', '')[:500]}")
        log.append(f"  ...remote status={st}")
        time.sleep(2)


def _download_from_s3(s3_bucket: str, s3_key: str) -> bytes:
    """Скачивает объект через pre-signed URL (обход багов Beget)."""
    s3 = _s3_client()
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": s3_bucket, "Key": s3_key},
        ExpiresIn=600,
    )
    resp = requests.get(url, timeout=300, stream=False)
    resp.raise_for_status()
    return resp.content


def _snapshot_tracking(job_id: str | None) -> dict | None:
    """Сохраняет текущие DevopsAgentJob + связанный DevopsAction перед drop schema.

    Эти записи лежат в той же схеме public, которую мы сейчас дропнем, и иначе
    пропадут — action в UI повиснет в running. Возвращаем dict-«снимок» для
    последующего _restore_tracking. Если job_id неизвестен или что-то пошло
    не так — возвращаем None (не падаем, это вспомогательная функциональность).
    """
    if not job_id:
        return None
    try:
        from apps.devops.models import DevopsAction, DevopsAgentJob
        snap: dict = {}
        job = DevopsAgentJob.objects.filter(pk=job_id).first()
        if job:
            snap["job"] = {
                "id": str(job.id),
                "action_type": job.action_type,
                "params": job.params or {},
                "status": job.status,
                "output": job.output or "",
                "result": job.result or {},
            }
        action = DevopsAction.objects.filter(remote_job_id=job_id).first()
        if action:
            snap["action"] = {
                "id": str(action.id),
                "environment_id": action.environment_id,
                "action_type": action.action_type,
                "status": action.status,
                "remote_job_id": action.remote_job_id or "",
                "output": action.output or "",
                "params": action.params or {},
                "started_by_id": action.started_by_id,
            }
        return snap or None
    except Exception:
        return None


def _restore_tracking(snap: dict | None, log: list[str]) -> None:
    """Возвращает строки DevopsAgentJob + DevopsAction после restore.

    Идемпотентно (update_or_create). Поля started_at/finished_at не трогаем —
    auto_now_add для started_at сработает на новой записи, finished_at будет
    проставлено финальным save'ом в run_agent_job уже после возврата handler'а.
    """
    if not snap:
        return
    try:
        from django.db import connections
        connections.close_all()
        from apps.devops.models import DevopsAction, DevopsAgentJob
        if snap.get("job"):
            j = snap["job"]
            DevopsAgentJob.objects.update_or_create(
                id=j["id"],
                defaults={
                    "action_type": j["action_type"],
                    "params": j["params"],
                    "status": j["status"],
                    "output": j["output"],
                    "result": j["result"],
                },
            )
        if snap.get("action"):
            a = snap["action"]
            DevopsAction.objects.update_or_create(
                id=a["id"],
                defaults={
                    "environment_id": a["environment_id"],
                    "action_type": a["action_type"],
                    "status": a["status"],
                    "remote_job_id": a["remote_job_id"],
                    "output": a["output"],
                    "params": a["params"],
                    "started_by_id": a["started_by_id"],
                },
            )
        log.append("  Tracking-записи action+job восстановлены после drop schema")
    except Exception as e:
        log.append(f"  ⚠ не удалось восстановить tracking: {e}")


def _post_restore_ensure_envs(log: list[str]) -> None:
    """После drop schema + restore возвращаем Environment-записи DevOps-панели.

    На источнике (откуда тянем дамп) этой таблицы может не быть вообще, или там
    могут быть другие записи — поэтому после restore вызываем devops_setup,
    который гарантирует наличие наших стандартных 'dev' и 'prod'.
    Не фатально: если что-то пошло не так — логируем, ручной re-run возможен.
    """
    log.append("\n=== Восстановление Environment-записей панели ===")
    try:
        # Старая ORM-коннекция могла указывать на дропнутую схему — заставим Django
        # переподключиться, иначе следующий запрос может упасть на «relation does not exist».
        from django.db import connections
        connections.close_all()

        from io import StringIO
        from django.core.management import call_command
        buf = StringIO()
        call_command("devops_setup", stdout=buf)
        for line in buf.getvalue().splitlines():
            log.append(f"  {line}")
    except Exception as e:
        log.append(f"  ⚠ devops_setup упал (не фатально, можно вручную): {e}")


def _restore_dump(sql_bytes: bytes, log: list[str]) -> None:
    """psql -c 'DROP SCHEMA' + восстанавливаем дамп через docker exec."""
    db_user = os.environ["POSTGRES_USER"]
    db_name = os.environ["POSTGRES_DB"]
    db_password = os.environ["POSTGRES_PASSWORD"]

    client = _docker_client()
    db_container = None
    for name in ["siricrm-db-1", "siricrm_db_1"]:
        try:
            db_container = client.containers.get(name)
            break
        except Exception:
            continue
    if db_container is None:
        raise RuntimeError("DB container not found")

    # Шаг 1: дроп схемы public
    log.append("  Дроп схемы public...")
    drop_sql = (
        "DROP SCHEMA IF EXISTS public CASCADE; "
        "CREATE SCHEMA public; "
        f"GRANT ALL ON SCHEMA public TO {db_user};"
    )
    exit_code, output = db_container.exec_run(
        cmd=["psql", "-U", db_user, "-d", db_name, "-c", drop_sql],
        environment={"PGPASSWORD": db_password},
    )
    if exit_code != 0:
        raise RuntimeError(f"DROP SCHEMA failed: {output[:500]!r}")

    # Шаг 2: применяем дамп через psql stdin
    log.append(f"  Восстановление {len(sql_bytes):,} байт SQL через psql stdin...")
    # Создаём exec instance, потом через socket пишем stdin
    exec_id = db_container.client.api.exec_create(
        db_container.id,
        cmd=["psql", "-U", db_user, "-d", db_name, "-q", "--single-transaction"],
        environment={"PGPASSWORD": db_password},
        stdin=True,
        stdout=True,
        stderr=True,
    )["Id"]
    sock = db_container.client.api.exec_start(exec_id, socket=True, demux=False)
    sock = sock._sock if hasattr(sock, "_sock") else sock
    try:
        sock.sendall(sql_bytes)
        sock.shutdown(1)  # SHUT_WR — psql завершит транзакцию
    except Exception as e:
        log.append(f"  socket write error: {e}")
    finally:
        sock.close()

    # Подождём завершения exec
    for _ in range(60):
        info = db_container.client.api.exec_inspect(exec_id)
        if not info["Running"]:
            if info["ExitCode"] != 0:
                raise RuntimeError(f"psql restore exited with code {info['ExitCode']}")
            break
        time.sleep(0.5)
    else:
        raise TimeoutError("psql restore did not finish in 30s")


@register_handler("pull_db")
def run_pull_db(params: dict) -> dict:
    source_env_id = params.get("source_env_id")
    if not source_env_id:
        raise ValueError("source_env_id required in params")

    source_env = Environment.objects.get(pk=source_env_id)
    log = [f"Источник: {source_env.name} ({source_env.base_url})"]

    # Снапшот tracking-записей (action+job) ДО любых разрушительных операций —
    # потом, после restore, мы их вернём в новую (чужую) схему, чтобы action
    # не повис в running на UI. См. _snapshot_tracking / _restore_tracking.
    tracking_snap = _snapshot_tracking(params.get("__job_id__"))

    # 0. Защитный бэкап текущей (dev) БД — на случай если что-то пойдёт не так.
    log.append("=== Защитный бэкап текущей БД ===")
    try:
        from apps.devops.handlers.backup import run_backup
        sb = run_backup({})
        log.append(sb.get("output", ""))
    except Exception as e:
        raise RuntimeError(f"Защитный бэкап не удался, pull_db отменён: {e}")

    # 1. Просим source_env сделать backup
    client = AgentClient(source_env)
    log.append("\n=== Запрос backup на источнике ===")
    remote_job = client.create_job("backup")
    remote_job_id = remote_job["id"]
    log.append(f"  remote job: {remote_job_id}")

    # 2. Ждём завершения
    log.append("Ожидание завершения backup...")
    finished_job = _wait_for_remote_job(client, remote_job_id, log)
    res = finished_job.get("result") or {}
    s3_bucket = res.get("s3_bucket")
    s3_key = res.get("s3_key")
    download_url = res.get("download_url")
    if not s3_key:
        raise RuntimeError(f"Backup did not return s3_key: {res}")
    log.append(f"  Backup готов: {s3_key} ({res.get('size_mb')} MB)")

    # 3. Скачиваем дамп: предпочитаем pre-signed URL от источника (не нужны его ключи)
    log.append("Скачивание дампа...")
    if download_url:
        resp = requests.get(download_url, timeout=300)
        resp.raise_for_status()
        gz_bytes = resp.content
    else:
        gz_bytes = _download_from_s3(s3_bucket, s3_key)
    log.append(f"  Скачано {len(gz_bytes):,} байт (gzip)")

    # 4. Распаковываем
    sql_bytes = gzip.decompress(gz_bytes)
    log.append(f"  Распаковано в {len(sql_bytes):,} байт SQL")

    # 5. Сохраняем локально для истории
    BACKUP_DIR.mkdir(exist_ok=True)
    local_path = BACKUP_DIR / Path(s3_key).name
    local_path.write_bytes(gz_bytes)
    log.append(f"  Сохранено локально: {local_path}")

    # 6. Restore на текущей БД
    log.append("ВНИМАНИЕ: дроп схемы public и restore...")
    _restore_dump(sql_bytes, log)
    log.append("  Restore выполнен")

    # 7. Возвращаем Environment-записи (могли уехать вместе с дампом).
    _post_restore_ensure_envs(log)

    # 8. Возвращаем tracking-записи self-action'а (action+job), чтобы UI не висел.
    _restore_tracking(tracking_snap, log)

    return {
        "output": "\n".join(log),
        "result": {
            "source_env": source_env.name,
            "s3_key": s3_key,
            "size_mb": res.get("size_mb"),
            "local_path": str(local_path),
            "finished_at": timezone.now().isoformat(),
        },
    }
