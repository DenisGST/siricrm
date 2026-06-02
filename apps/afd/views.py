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
    ctx = {
        "executors": ExecutorOrg.objects.all(),
        "templates": DocumentTemplate.objects.select_related("stored_file").all(),
        "recent": GeneratedDocument.objects.select_related(
            "client", "service", "pdf_file", "docx_file"
        )[:20],
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
