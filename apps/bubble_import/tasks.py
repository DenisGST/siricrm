"""Фоновые celery-задачи импорта из Bubble.io.

Запускаются из UI кнопкой «Импортировать ВСЁ». Прогресс пишется в
BubbleImportJob — UI поллит его HTMX-ом каждые ~2 секунды.
"""
import logging
import traceback

from celery import shared_task
from django.utils import timezone

from . import bubble_api
from .appliers import apply_record, link_spouses
from .models import BubbleImportJob, BubbleRecord
from .services import fetch_batch, get_state

logger = logging.getLogger("bubble_import")

# Размер порций.
FETCH_BATCH = 100      # max Bubble Data API
APPLY_REPORT_EVERY = 50  # как часто обновлять счётчики при apply


def _refresh_cancel(job: BubbleImportJob) -> bool:
    """Перечитать cancel_requested из БД (не кэш экземпляра)."""
    job.refresh_from_db(fields=["cancel_requested"])
    return job.cancel_requested


def _finish(job: BubbleImportJob, status: str):
    job.status = status
    job.current_action = ""
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "current_action", "finished_at"])


@shared_task(
    bind=True,
    name="bubble_import.full_import",
    # Bubble-импорт большой сущности (Files 546k, MessageWSP 258k со
    # скачиванием медиа) может идти часами. Глобальный CELERY_TASK_TIME_LIMIT
    # 30 мин (а на prod ещё короче, 5 мин) убивает task через SIGKILL —
    # явно задаём 24-часовой лимит на эту задачу.
    time_limit=24 * 60 * 60,
    soft_time_limit=24 * 60 * 60 - 60,
)
def full_import_task(self, job_id: str):
    """Полный импорт одной сущности: fetch всех порций → одобрить новое →
    apply одобренного. Прогресс пишется в BubbleImportJob."""
    try:
        job = BubbleImportJob.objects.get(pk=job_id)
    except BubbleImportJob.DoesNotExist:
        logger.warning("full_import_task: job %s не найден", job_id)
        return

    entity = job.entity
    job.status = "running"
    job.celery_task_id = self.request.id or ""
    job.save(update_fields=["status", "celery_task_id"])
    job.add_log(f"Старт импорта сущности {entity}")

    if not bubble_api.is_configured():
        job.add_log("ОШИБКА: не настроен BUBBLE_API_TOKEN")
        _finish(job, "error")
        return

    try:
        # ─── Этап 1: FETCH ALL ───────────────────────────
        job.current_action = "Загрузка из Bubble"
        job.save(update_fields=["current_action"])
        job.add_log("Этап 1/3: загрузка из Bubble")

        page = 0
        while True:
            if _refresh_cancel(job):
                job.add_log("Отменено пользователем")
                _finish(job, "cancelled")
                return
            try:
                res = fetch_batch(entity, batch=FETCH_BATCH)
            except bubble_api.BubbleAPIError as e:
                job.add_log(f"Bubble API ошибка: {e}")
                raise
            page += 1
            state = get_state(entity)
            job.fetched_total = state.total_fetched
            job.remote_total = state.total_remote
            job.save(update_fields=["fetched_total", "remote_total"])
            job.add_log(
                f"  стр.{page}: +{res['created']} новых, "
                f"{res['updated']} обновлено, всего {state.total_fetched}/{state.total_remote}"
            )
            if res["remaining"] <= 0 or res["fetched"] == 0:
                break

        # ─── Этап 2: APPROVE ALL ─────────────────────────
        if _refresh_cancel(job):
            job.add_log("Отменено пользователем")
            _finish(job, "cancelled")
            return
        job.current_action = "Одобрение новых записей"
        job.save(update_fields=["current_action"])
        n_approved = BubbleRecord.objects.filter(entity=entity).exclude(
            status="imported"
        ).update(approved=True)
        job.add_log(f"Этап 2/3: одобрено {n_approved} записей")

        # ─── Этап 3: APPLY ───────────────────────────────
        job.current_action = "Применение в SiriCRM"
        job.save(update_fields=["current_action"])
        qs = BubbleRecord.objects.filter(
            entity=entity, approved=True,
        ).exclude(status="imported")
        total = qs.count()
        job.add_log(f"Этап 3/3: применение, к импорту {total}")

        i = imp = errs = skp = 0
        for rec in qs.iterator(chunk_size=100):
            if _refresh_cancel(job):
                job.add_log(f"Отменено после {i} из {total}")
                _finish(job, "cancelled")
                return
            i += 1
            st = apply_record(rec)
            if st == "imported":
                imp += 1
            elif st == "skipped":
                skp += 1
            else:
                errs += 1
            if i % APPLY_REPORT_EVERY == 0:
                job.applied_count = imp
                job.errors_count = errs
                job.skipped_count = skp
                job.save(update_fields=["applied_count", "errors_count", "skipped_count"])
                job.add_log(f"  применено {i}/{total} (импорт {imp}, ошибки {errs}, пропуск {skp})")

        job.applied_count = imp
        job.errors_count = errs
        job.skipped_count = skp
        job.save(update_fields=["applied_count", "errors_count", "skipped_count"])

        # Дополнительно для клиентов — связь супругов.
        if entity == "Man":
            n = link_spouses()
            job.add_log(f"Связано супругов: {n}")

        job.add_log(
            f"ГОТОВО: импортировано {imp}, ошибок {errs}, пропущено {skp}"
        )
        _finish(job, "done")

    except Exception as e:  # noqa: BLE001 — фоновая задача, всё в Job
        job.refresh_from_db()
        job.error_text = traceback.format_exc()[-4000:]
        job.add_log(f"СБОЙ: {type(e).__name__}: {e}", save=False)
        job.status = "error"
        job.current_action = ""
        job.finished_at = timezone.now()
        job.save(update_fields=[
            "status", "current_action", "finished_at", "error_text", "log_text",
        ])
        logger.exception("full_import_task %s failed", entity)
