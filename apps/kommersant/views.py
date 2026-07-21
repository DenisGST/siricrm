"""Вьюхи под-вкладки «КоммерсантЪ» (вкладка «Публикации» карточки дела БФЛ).

Поток юриста: выбрал тип → сформировал текст → собрал заявку (бланк ИД с факсимиле)
→ приложил подтверждающие документы → «Запросить счёт» (письмо в ИД) → счёт приходит
ответом на письмо и подтягивается сам → отметил оплату → вписал факт публикации.

🛑 Сетевые операции (SMTP/IMAP) во вьюхах НЕ выполняются — только постановка
   Celery-задачи (см. tasks.py).
Гейт/гард услуги БФЛ переиспользуем из apps.procedure, как в apps.efrsb.
"""
from __future__ import annotations

import json
import logging

from django.http import HttpResponse, HttpResponseForbidden
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3
from apps.procedure import services as proc_services
from apps.procedure.permissions import require_procedures
from apps.procedure.views import _NotBFL, _bfl_service

from . import services
from .blank import BLANK_REQUIRED_KEYS, KommersantBlankError, generate_blank, save_text
from .generator import KommersantGenError, check_problems, render_message_text
from .models import KOMMERSANT_EMAIL, KommersantAttachment, KommersantMessageType, KommersantPublication

log = logging.getLogger(__name__)


def _actor(request):
    return getattr(request.user, "employee", None)


def _case(request, service_id):
    service = _bfl_service(request, service_id)
    return service, proc_services.ensure_case(service)


def _toast(msg: str, kind: str = "info"):
    """204 + перерисовка реестра + тост (слушатель — kommersantToast)."""
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = json.dumps({
        "reloadKommersant": True, "kommersantToast": {"msg": msg, "kind": kind},
    })
    return resp


def _close_modal(msg: str, kind: str = "success"):
    """Пустой ответ (модалка схлопывается outerHTML-свопом) + тост."""
    resp = HttpResponse("")
    resp["HX-Trigger"] = json.dumps({
        "reloadKommersant": True, "kommersantToast": {"msg": msg, "kind": kind},
    })
    return resp


# ── Реестр ──────────────────────────────────────────────────────────────────

def _context(service, case) -> dict:
    proc = case.current_procedure
    types = list(KommersantMessageType.objects.filter(is_active=True).order_by("order", "name"))
    publications = list(
        case.kommersant_publications
        .select_related("message_type", "procedure", "blank_pdf", "blank_docx", "invoice_file")
        .prefetch_related("attachments__stored_file")
        .all()
    )
    am = proc.arbitr_manager if (proc and proc.arbitr_manager_id) else None
    account = services.mail_account_for_manager(am)
    return {
        "service": service,
        "case": case,
        "current_procedure": proc,
        "proc_kind": (proc.kind if proc is not None else ""),
        "message_types": types,
        "types_json": json.dumps([
            {"tid": str(t.id), "name": t.name, "kinds": list(t.applicable_kinds or [])}
            for t in types
        ]),
        "publications": publications,
        "manager": am,
        "manager_has_employee": bool(am and am.employee_id),
        "mail_ready": account is not None,
        "imap_ready": bool(account and account.has_imap),
        "kommersant_email": KOMMERSANT_EMAIL,
    }


@never_cache
@login_required
@require_procedures
def subtab(request, service_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    return render(request, "kommersant/_subtab_kommersant.html", _context(service, case))


# ── Текст сообщения ─────────────────────────────────────────────────────────

def _pick_type(request):
    mt = KommersantMessageType.objects.filter(
        pk=(request.POST.get("message_type") or None)
    ).first()
    if mt is None:
        return None, _toast("Выберите тип сообщения.", "warning")
    return mt, None


def _text_modal(request, service, case, mt, *, pub=None, text="", error=""):
    problems = check_problems(case, mt, procedure=case.current_procedure)
    return render(request, "kommersant/_publication_form_modal.html", {
        "service": service, "case": case, "mt": mt, "pub": pub,
        "problems": problems, "text_value": text, "error": error,
        "type_label": mt.name,
    })


@login_required
@require_procedures
@require_POST
def publication_add(request, service_id):
    """«+ Добавить сообщение» → модалка. Запись создаётся только по «Сохранить»."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    mt, err = _pick_type(request)
    if err is not None:
        return err
    return _text_modal(request, service, case, mt)


@never_cache
@login_required
@require_procedures
def publication_edit(request, service_id, pub_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    return _text_modal(request, service, case, pub.message_type, pub=pub, text=pub.text or "")


@login_required
@require_procedures
@require_POST
def publication_gen_text(request, service_id):
    """«Сгенерировать текст» — подставляет данные CRM в шаблон типа."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    mt, err = _pick_type(request)
    if err is not None:
        return err
    try:
        text = render_message_text(case, mt, procedure=case.current_procedure)
    except KommersantGenError as exc:
        resp = render(request, "kommersant/_publication_text_field.html",
                      {"text_value": (request.POST.get("text") or "")})
        resp["HX-Trigger"] = json.dumps(
            {"kommersantToast": {"msg": str(exc), "kind": "warning"}}
        )
        return resp
    return render(request, "kommersant/_publication_text_field.html", {"text_value": text})


@login_required
@require_procedures
@require_POST
def publication_save(request, service_id):
    """«Сохранить»: создать/обновить публикацию с итоговым текстом."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    mt, err = _pick_type(request)
    if err is not None:
        return err

    text = (request.POST.get("text") or "").strip()
    pub = None
    if request.POST.get("pub_id"):
        pub = KommersantPublication.objects.filter(
            pk=request.POST["pub_id"], case=case).first()
    if not text:
        return _text_modal(request, service, case, mt, pub=pub, text=text,
                           error="Текст сообщения пуст — сгенерируйте его или введите вручную.")
    if pub is None:
        pub = services.create_publication(
            case, mt, procedure=case.current_procedure, employee=_actor(request))
    else:
        pub.message_type = mt

    docs_to = request.POST.get("accounting_docs_to")
    if docs_to in dict(KommersantPublication.DOCS_TO_CHOICES):
        pub.accounting_docs_to = docs_to

    try:
        save_text(pub, text, employee=_actor(request))
    except KommersantBlankError as exc:
        return _text_modal(request, service, case, mt, pub=pub, text=text, error=str(exc))
    return _close_modal("Текст сообщения сохранён. Следующий шаг — сформировать заявку.")


@never_cache
@login_required
@require_procedures
def publication_text(request, service_id, pub_id):
    """Модалка «текст целиком» с кнопкой копирования."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    return render(request, "kommersant/_publication_text_modal.html",
                  {"service": service, "pub": pub})


@login_required
@require_procedures
@require_POST
def publication_delete(request, service_id, pub_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    if pub.status in (KommersantPublication.STATUS_DRAFT,
                      KommersantPublication.STATUS_GENERATED):
        pub.delete()
        return _toast("Публикация удалена.")
    # Отправленную заявку не удаляем: в ИД она уже есть, а по счёту пойдут деньги.
    return _toast("Заявка уже отправлена в ИД — удалить нельзя, отмените её.", "warning")


@login_required
@require_procedures
@require_POST
def publication_cancel(request, service_id, pub_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    pub.status = KommersantPublication.STATUS_CANCELLED
    pub.save(update_fields=["status", "updated_at"])
    return _toast("Публикация отменена.")


# ── Заявка (бланк) и вложения ───────────────────────────────────────────────

@login_required
@require_procedures
@require_POST
def blank_generate(request, service_id, pub_id):
    """«Сформировать заявку» — бланк ИД с подстановкой данных и факсимиле."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    try:
        generate_blank(pub, employee=_actor(request))
    except KommersantBlankError as exc:
        return _toast(str(exc), "error")
    except Exception:
        log.exception("kommersant blank_generate failed")
        return _toast("Не удалось сформировать заявку (ошибка конвертации в PDF).", "error")
    return _toast("Заявка сформирована — проверьте PDF перед отправкой.", "success")


@never_cache
@login_required
@require_procedures
def send_modal(request, service_id, pub_id):
    """Предпроверка перед отправкой: что не заполнено, что приложено, куда уйдёт."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    am = services.manager_for(pub)
    account = services.mail_account_for_manager(am)
    problems = check_problems(case, pub.message_type, procedure=pub.procedure,
                              keys=BLANK_REQUIRED_KEYS)
    blockers = []
    if not (pub.text or "").strip():
        blockers.append("не сформирован текст сообщения")
    if pub.blank_pdf_id is None or pub.blank_docx_id is None:
        blockers.append("не сформирован бланк заявки")
    if am is None:
        blockers.append("в процедуре не назначен арбитражный управляющий")
    elif not am.employee_id:
        blockers.append(f"АУ {am.short_fio} не привязан к сотруднику — некому подать заявку")
    elif account is None:
        blockers.append(
            f"у АУ {am.short_fio} не настроена почта для «Коммерсанта» "
            f"(его профиль → «Почта для публикаций в «Коммерсантъ»)"
        )
    if not pub.attachments.exists():
        blockers.append("не приложены подтверждающие документы — ИД откажет в публикации")

    return render(request, "kommersant/_send_modal.html", {
        "service": service, "pub": pub, "manager": am, "account": account,
        "problems": problems, "blockers": blockers,
        "kommersant_email": KOMMERSANT_EMAIL,
        "attachments": pub.attachments.select_related("stored_file").all(),
    })


@login_required
@require_procedures
@require_POST
def attachment_add(request, service_id, pub_id):
    """Приложить подтверждающий документ к заявке (судебный акт, полномочия АУ)."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)

    files = request.FILES.getlist("files")
    if not files:
        return _toast("Файл не выбран.", "warning")
    kind = request.POST.get("kind") or KommersantAttachment.KIND_OTHER
    for upload in files:
        data = upload.read()
        bucket, key = upload_file_to_s3(
            data, prefix="kommersant/attachments", filename=upload.name,
            content_type=upload.content_type or "application/octet-stream",
        )
        stored = StoredFile.objects.create(
            bucket=bucket, key=key, filename=upload.name,
            content_type=upload.content_type or "application/octet-stream", size=len(data),
        )
        KommersantAttachment.objects.create(
            publication=pub, stored_file=stored, kind=kind)
    return _toast(f"Приложено файлов: {len(files)}.", "success")


@login_required
@require_procedures
@require_POST
def attachment_delete(request, service_id, pub_id, att_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    KommersantAttachment.objects.filter(pk=att_id, publication=pub).delete()
    return _toast("Вложение удалено.")


# ── Отправка и счёт ─────────────────────────────────────────────────────────

@login_required
@require_procedures
@require_POST
def send_request(request, service_id, pub_id):
    """«Запросить счёт» — ставит Celery-задачу на отправку письма в ИД."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    if pub.status == KommersantPublication.STATUS_SENT:
        return _toast("Заявка уже отправлена — ожидаем счёт.", "warning")

    from .tasks import send_request as send_task
    employee = _actor(request)
    send_task.delay(str(pub.pk), employee.pk if employee else None,
                    request.POST.get("to_addr") or KOMMERSANT_EMAIL)
    return _close_modal(
        "Заявка отправляется. Счёт придёт ответом на письмо и подтянется в карточку сам.")


@login_required
@require_procedures
@require_POST
def fetch_invoice(request, service_id, pub_id):
    """«Проверить счёт сейчас» — разовый обход почты вне расписания."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    account = services.mail_account_for(pub)
    if account is None or not account.has_imap:
        return _toast("У АУ не настроена почта для приёма счёта (профиль сотрудника-АУ).", "warning")

    from .tasks import fetch_invoice_now
    fetch_invoice_now.delay(str(pub.pk))
    return _toast("Проверяем почту — если счёт пришёл, он появится в карточке.", "info")


@never_cache
@login_required
@require_procedures
def invoice_modal(request, service_id, pub_id):
    """Реквизиты счёта и отметка оплаты (правятся руками — ИД шлёт счёт файлом)."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    return render(request, "kommersant/_invoice_modal.html", {"service": service, "pub": pub})


@login_required
@require_procedures
@require_POST
def invoice_save(request, service_id, pub_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)

    pub.invoice_number = (request.POST.get("invoice_number") or "").strip()[:64]
    pub.invoice_date = request.POST.get("invoice_date") or None
    amount = (request.POST.get("invoice_amount") or "").replace(",", ".").strip()
    pub.invoice_amount = amount or None
    pub.is_paid = bool(request.POST.get("is_paid"))
    pub.paid_date = request.POST.get("paid_date") or None
    if pub.is_paid and pub.paid_date is None:
        pub.paid_date = timezone.localdate()
    if pub.is_paid and pub.status in (KommersantPublication.STATUS_SENT,
                                      KommersantPublication.STATUS_INVOICED):
        pub.status = KommersantPublication.STATUS_PAID
    pub.save()
    return _close_modal("Счёт сохранён.")


@never_cache
@login_required
@require_procedures
def published_modal(request, service_id, pub_id):
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)
    return render(request, "kommersant/_published_modal.html", {"service": service, "pub": pub})


@login_required
@require_procedures
@require_POST
def published_save(request, service_id, pub_id):
    """Отметить выход публикации + записать дату в процедуру (якорь сроков)."""
    try:
        service, case = _case(request, service_id)
    except _NotBFL as exc:
        return HttpResponseForbidden(str(exc))
    pub = get_object_or_404(KommersantPublication, pk=pub_id, case=case)

    pub.publication_date = request.POST.get("publication_date") or None
    pub.newspaper_number = (request.POST.get("newspaper_number") or "").strip()[:64]
    pub.announcement_number = (request.POST.get("announcement_number") or "").strip()[:64]
    if pub.publication_date:
        pub.status = KommersantPublication.STATUS_PUBLISHED
    pub.save()

    # Дата публикации в «Коммерсанте» — якорь для мероприятий-сроков процедуры,
    # поэтому дублируем её в Procedure и пересчитываем дедлайны.
    proc = pub.procedure
    if proc is not None and pub.publication_date:
        proc.publication_kommersant_date = pub.publication_date
        proc.save(update_fields=["publication_kommersant_date", "updated_at"])
        try:
            proc_services.recompute_due_dates(proc)
        except Exception:
            log.exception("published_save: не пересчитались сроки процедуры %s", proc.pk)

    return _close_modal("Публикация отмечена. Сроки процедуры пересчитаны.")
