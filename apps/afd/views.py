"""Views АФД: панель управления, CRUD исполнителей/шаблонов, генерация договора."""
import logging

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.permissions import get_employee, require_references_access
from apps.crm import client_log
from apps.crm.models import Message, Service
from apps.files.models import StoredFile
from apps.files.s3_utils import upload_file_to_s3

from . import contract_bfl
from .generator import ContractGenerationError, generate_bfl_contract
from .models import DocumentTemplate, ExecutorOrg, GeneratedDocument

# Каналы отправки договора клиенту.
_CHANNEL_TASKS = {
    "telegram": ("apps.crm.tasks", "send_telegram_message_task"),
    "max": ("apps.crm.tasks", "send_max_message_task"),
    "whatsapp": ("apps.whatsapp.tasks", "send_whatsapp_message_task"),
}


def _channel_ctx(client):
    """Доступность каналов + канал последнего входящего сообщения (для подсветки)."""
    has_wa = bool(
        client.whatsapp_phone
        or client.phone
        or client.phones.filter(purpose__in=["whatsapp", "primary"]).exists()
    )
    last_in = (
        Message.objects.filter(client=client, direction="incoming")
        .order_by("-created_at")
        .values_list("channel", flat=True)
        .first()
    )
    return {
        "can_telegram": bool(client.telegram_id),
        "can_max": bool(client.max_chat_id),
        "can_whatsapp": has_wa,
        "last_incoming_channel": last_in,
    }

log = logging.getLogger(__name__)

_DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _panel_reload():
    """204 + HX-Trigger — закрывает модалку и перезагружает панель АФД."""
    return HttpResponse(status=204, headers={"HX-Trigger": "afdPanelChanged"})


# ── Формы ─────────────────────────────────────────────────────────────────────
class ExecutorOrgForm(forms.ModelForm):
    class Meta:
        model = ExecutorOrg
        fields = ["name", "intro_text", "requisites", "signer_name",
                  "is_default", "is_active"]
        widgets = {
            "intro_text": forms.Textarea(attrs={"rows": 3, "class": "textarea textarea-bordered w-full"}),
            "requisites": forms.Textarea(attrs={"rows": 6, "class": "textarea textarea-bordered w-full"}),
            "name": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "signer_name": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
        }


class DocumentTemplateForm(forms.ModelForm):
    docx = forms.FileField(label="Файл шаблона (.docx)", required=False)

    class Meta:
        model = DocumentTemplate
        fields = ["name", "kind", "description", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "kind": forms.Select(attrs={"class": "select select-bordered w-full"}),
            "description": forms.Textarea(attrs={"rows": 4, "class": "textarea textarea-bordered w-full"}),
        }


# ── Панель ────────────────────────────────────────────────────────────────────
@login_required
@require_references_access
def panel(request):
    from .models import IskTemplate
    isk_tpl = IskTemplate.get_default()
    ctx = {
        "executors": ExecutorOrg.objects.all(),
        "templates": DocumentTemplate.objects.select_related("stored_file").all(),
        "recent": GeneratedDocument.objects.select_related(
            "client", "service", "pdf_file", "docx_file"
        )[:20],
        "isk_template": isk_tpl,
        "isk_sections": isk_tpl.sections.order_by("order") if isk_tpl else [],
    }
    return render(request, "afd/panel.html", ctx)


# ── CRUD исполнителей ───────────────────────────────────────────────────────────
@login_required
@require_references_access
def executor_edit(request, pk=None):
    obj = get_object_or_404(ExecutorOrg, pk=pk) if pk else None
    if request.method == "POST":
        form = ExecutorOrgForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return _panel_reload()
    else:
        form = ExecutorOrgForm(instance=obj)
    return render(request, "afd/_executor_form.html", {"form": form, "obj": obj})


@login_required
@require_references_access
@require_POST
def executor_delete(request, pk):
    get_object_or_404(ExecutorOrg, pk=pk).delete()
    return _panel_reload()


# ── CRUD шаблонов ────────────────────────────────────────────────────────────────
@login_required
@require_references_access
def template_edit(request, pk=None):
    obj = get_object_or_404(DocumentTemplate, pk=pk) if pk else None
    if request.method == "POST":
        form = DocumentTemplateForm(request.POST, request.FILES, instance=obj)
        if form.is_valid():
            tpl = form.save(commit=False)
            upload = form.cleaned_data.get("docx")
            if upload:
                data = upload.read()
                bucket, key = upload_file_to_s3(
                    data, prefix="afd/templates", filename=upload.name, content_type=_DOCX_CT,
                )
                tpl.stored_file = StoredFile.objects.create(
                    bucket=bucket, key=key, filename=upload.name,
                    content_type=_DOCX_CT, size=len(data),
                )
            if not tpl.stored_file_id:
                form.add_error("docx", "Загрузите .docx-файл шаблона.")
                return render(request, "afd/_template_form.html", {"form": form, "obj": obj})
            tpl.updated_by = get_employee(request.user)
            tpl.save()
            return _panel_reload()
    else:
        form = DocumentTemplateForm(instance=obj)
    return render(request, "afd/_template_form.html", {"form": form, "obj": obj})


@login_required
@require_references_access
@require_POST
def template_delete(request, pk):
    get_object_or_404(DocumentTemplate, pk=pk).delete()
    return _panel_reload()


# ── Договор: проверка реквизитов + генерация ─────────────────────────────────────
@login_required
def contract_check(request, service_id):
    service = get_object_or_404(
        Service.objects.select_related("client", "region"), pk=service_id
    )
    ok, groups = contract_bfl.check_requisites(service)
    template = DocumentTemplate.active_for_kind(DocumentTemplate.KIND_CONTRACT_BFL)
    return render(request, "afd/contract_modal.html", {
        "service": service, "ok": ok, "groups": groups,
        "has_template": template is not None,
    })


@login_required
@require_POST
def contract_generate(request, service_id):
    service = get_object_or_404(
        Service.objects.select_related("client", "region"), pk=service_id
    )
    employee = get_employee(request.user)
    try:
        gen = generate_bfl_contract(service, employee)
    except ContractGenerationError as e:
        return render(request, "afd/_contract_result.html",
                      {"service": service, "error": str(e)})
    except Exception:
        log.exception("contract_generate: ошибка генерации договора")
        return render(request, "afd/_contract_result.html",
                      {"service": service, "error": "Внутренняя ошибка при генерации. См. логи."})
    ctx = {"service": service, "gen": gen}
    ctx.update(_channel_ctx(service.client))
    return render(request, "afd/_contract_result.html", ctx)


@login_required
@require_POST
def contract_send(request, gen_id, channel):
    """Отправить сформированный договор (PDF) клиенту через выбранный канал."""
    gen = get_object_or_404(
        GeneratedDocument.objects.select_related("client", "service", "pdf_file"),
        pk=gen_id,
    )
    client = gen.client
    service = gen.service
    base_ctx = {"service": service, "gen": gen}
    base_ctx.update(_channel_ctx(client))

    if channel not in _CHANNEL_TASKS:
        return HttpResponseBadRequest("Неизвестный канал")
    stored = gen.pdf_file
    if stored is None:
        base_ctx["send_error"] = "Нет PDF для отправки."
        return render(request, "afd/_contract_result.html", base_ctx)

    # Проверка доступности канала у клиента.
    avail = {"telegram": base_ctx["can_telegram"], "max": base_ctx["can_max"],
             "whatsapp": base_ctx["can_whatsapp"]}
    if not avail.get(channel):
        base_ctx["send_error"] = "У клиента нет контакта в этом канале."
        return render(request, "afd/_contract_result.html", base_ctx)

    employee = get_employee(request.user)
    client.last_message_at = timezone.now()
    client.save(update_fields=["last_message_at"])

    msg = Message.objects.create(
        client=client, employee=employee,
        content=f"Договор {gen.title}. Направляем на ознакомление.",
        direction="outgoing", channel=channel, message_type="document",
        file=stored, file_name=stored.filename,
        telegram_date=timezone.now(), is_sent=False,
    )

    import importlib
    mod_name, task_name = _CHANNEL_TASKS[channel]
    task = getattr(importlib.import_module(mod_name), task_name)
    task.delay(str(msg.id))

    # Событийка: «Отправлен договор клиенту» со ссылкой на файл договора.
    channel_name = {"telegram": "Telegram", "max": "MAX", "whatsapp": "WhatsApp"}[channel]
    try:
        client_log.record_action(
            client, "file_sent",
            comment=f"Отправлен договор «{gen.title}» клиенту через {channel_name}",
            employee=employee, stored_file=stored,
        )
    except Exception:
        log.exception("contract_send: не удалось записать событийку об отправке")

    base_ctx["sent_channel"] = channel
    return render(request, "afd/_contract_result.html", base_ctx)


# ═══════════════════════════════════════════════════════════════════════════
# Заявление о банкротстве (иск) — секционный конструктор + генерация
# ═══════════════════════════════════════════════════════════════════════════
class IskSectionForm(forms.ModelForm):
    class Meta:
        from .models import IskSection
        model = IskSection
        fields = ["title", "block_type", "align", "bold", "body",
                  "is_optional", "include_condition", "is_active"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "input input-bordered input-sm w-full"}),
            "block_type": forms.Select(attrs={"class": "select select-bordered select-sm w-full"}),
            "align": forms.Select(attrs={"class": "select select-bordered select-sm w-full"}),
            "include_condition": forms.TextInput(attrs={"class": "input input-bordered input-sm w-full"}),
            "body": forms.Textarea(attrs={"rows": 8, "class": "textarea textarea-bordered w-full font-mono text-xs"}),
        }


@login_required
@require_references_access
def isk_section_edit(request, pk=None):
    from .models import IskSection, IskTemplate
    obj = get_object_or_404(IskSection, pk=pk) if pk else None
    if request.method == "POST":
        form = IskSectionForm(request.POST, instance=obj)
        if form.is_valid():
            sec = form.save(commit=False)
            if obj is None:
                tpl = IskTemplate.get_default()
                if tpl is None:
                    tpl = IskTemplate.objects.create(name="Заявление о банкротстве (БФЛ)",
                                                     is_default=True)
                sec.template = tpl
                last = tpl.sections.order_by("-order").first()
                sec.order = (last.order + 10) if last else 10
            sec.save()
            return _panel_reload()
    else:
        form = IskSectionForm(instance=obj)
    return render(request, "afd/_isk_section_form.html", {"form": form, "obj": obj})


@login_required
@require_references_access
@require_POST
def isk_section_delete(request, pk):
    from .models import IskSection
    get_object_or_404(IskSection, pk=pk).delete()
    return _panel_reload()


@login_required
@require_references_access
@require_POST
def isk_section_move(request, pk, direction):
    """Переместить раздел вверх/вниз (обмен порядком с соседом)."""
    from .models import IskSection
    sec = get_object_or_404(IskSection, pk=pk)
    siblings = list(sec.template.sections.order_by("order"))
    idx = next((i for i, s in enumerate(siblings) if s.pk == sec.pk), None)
    swap = None
    if direction == "up" and idx > 0:
        swap = siblings[idx - 1]
    elif direction == "down" and idx is not None and idx < len(siblings) - 1:
        swap = siblings[idx + 1]
    if swap:
        sec.order, swap.order = swap.order, sec.order
        sec.save(update_fields=["order"])
        swap.save(update_fields=["order"])
    return _panel_reload()


def _isk_review_ctx(service, overrides=None, response_id=None):
    from django.conf import settings
    from . import isk_context
    responses = list(service.questionnaire_responses
                     .select_related("template").order_by("-is_complete", "-updated_at"))
    chosen = None
    if response_id:
        chosen = next((r for r in responses if str(r.id) == str(response_id)), None)
    if chosen is None:
        chosen = responses[0] if responses else None
    ctx, flags, creditors, warnings = isk_context.build_isk_context(
        service, overrides=overrides or {}, response=chosen)
    from .isk_seed_data import DEFAULT_APPENDIX
    appendix = []
    for a in DEFAULT_APPENDIX:
        cond = a.get("cond")
        appendix.append({**a, "checked": (not cond) or flags.get(cond, False)})
    return {
        "service": service, "client": service.client,
        "creditors": creditors, "warnings": warnings, "flags": flags,
        "appendix": appendix,
        "responses": responses, "chosen_id": (str(chosen.id) if chosen else ""),
        "dadata_api_key": settings.DADATA_API_KEY,
        "missing_count": sum(1 for c in creditors
                             if c["kind"] in ("bank", "mfo") and not c["has_requisites"]),
    }


@login_required
def isk_review(request, service_id):
    service = get_object_or_404(
        Service.objects.select_related("client", "region"), pk=service_id)
    rid = request.GET.get("response_id")
    return render(request, "afd/isk_review_modal.html", _isk_review_ctx(service, response_id=rid))


@login_required
def isk_creditors(request, service_id):
    """Перечень кредиторов для выбранной анкеты (обновляется при смене анкеты)."""
    service = get_object_or_404(
        Service.objects.select_related("client", "region"), pk=service_id)
    rid = request.GET.get("response_id")
    return render(request, "afd/_isk_creditors.html", _isk_review_ctx(service, response_id=rid))


@login_required
@require_POST
def isk_generate(request, service_id):
    from .isk_generator import IskGenerationError, generate_isk
    service = get_object_or_404(
        Service.objects.select_related("client", "region"), pk=service_id)
    employee = get_employee(request.user)

    overrides = {
        "employer": request.POST.get("employer", "").strip(),
        "former_name": request.POST.get("former_name", "").strip(),
        "accounts_block": request.POST.get("accounts_block", "").strip(),
        "property_cash": request.POST.get("property_cash", "").strip(),
        "property_text": request.POST.get("property_text", "").strip(),
        "appendix_keys": request.POST.getlist("appendix_keys"),
    }
    # пустой property_text → не переопределять (берём авто)
    if not overrides["property_text"]:
        overrides.pop("property_text")
    sro_id = request.POST.get("sro_id") or None
    response_id = request.POST.get("response_id") or None

    try:
        gen, warnings, creditors = generate_isk(
            service, employee=employee, overrides=overrides, sro_id=sro_id,
            response_id=response_id)
    except IskGenerationError as e:
        return render(request, "afd/_isk_result.html",
                      {"service": service, "error": str(e)})
    except Exception:
        log.exception("isk_generate: ошибка генерации заявления")
        return render(request, "afd/_isk_result.html",
                      {"service": service, "error": "Внутренняя ошибка. См. логи."})
    return render(request, "afd/_isk_result.html",
                  {"service": service, "gen": gen, "warnings": warnings,
                   "creditors_count": len(creditors)})
