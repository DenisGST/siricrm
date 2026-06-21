from django.contrib import admin

from .models import (
    BankruptcyCase,
    MilestoneTemplate,
    Procedure,
    ProcedureMilestone,
    ProcedureStage,
    Request,
    RequestPackage,
    RequestType,
)


@admin.register(ProcedureStage)
class ProcedureStageAdmin(admin.ModelAdmin):
    list_display = ("order", "name", "code", "kind_scope", "is_terminal", "is_active")
    list_display_links = ("name",)
    list_editable = ("order", "is_active")
    list_filter = ("kind_scope", "is_active")
    search_fields = ("name", "code")
    ordering = ("order",)


@admin.register(MilestoneTemplate)
class MilestoneTemplateAdmin(admin.ModelAdmin):
    list_display = (
        "order", "title", "stage", "base_date_key", "offset_days",
        "is_mandatory", "is_draft", "is_active",
    )
    list_display_links = ("title",)
    list_editable = ("order", "offset_days", "is_mandatory", "is_draft", "is_active")
    list_filter = ("stage", "is_draft", "is_mandatory", "is_active")
    search_fields = ("title", "code")
    ordering = ("stage__order", "order")


class ProcedureInline(admin.TabularInline):
    model = Procedure
    extra = 0
    fields = ("order", "kind", "current_stage", "intro_date",
              "publication_efrsb_date", "publication_kommersant_date", "outcome")
    show_change_link = True


@admin.register(BankruptcyCase)
class BankruptcyCaseAdmin(admin.ModelAdmin):
    list_display = ("service", "status", "current_stage", "fm_display", "updated_at")
    list_filter = ("status", "current_stage", "first_hearing_outcome")
    search_fields = ("service__numb_dogovor", "service__client__last_name")
    raw_id_fields = ("service", "current_procedure")
    inlines = [ProcedureInline]


@admin.register(Procedure)
class ProcedureAdmin(admin.ModelAdmin):
    list_display = ("case", "kind", "order", "current_stage", "financial_manager", "outcome", "updated_at")
    list_filter = ("kind", "current_stage", "outcome")
    search_fields = ("case__service__numb_dogovor", "case__service__client__last_name")
    raw_id_fields = ("case", "current_stage", "financial_manager")


@admin.register(ProcedureMilestone)
class ProcedureMilestoneAdmin(admin.ModelAdmin):
    list_display = ("title", "case", "procedure", "stage", "due_date", "status", "is_manual")
    list_filter = ("status", "stage", "is_mandatory", "is_manual")
    search_fields = ("title", "case__service__numb_dogovor")
    raw_id_fields = ("case", "procedure", "template", "responsible", "done_by")


@admin.register(RequestType)
class RequestTypeAdmin(admin.ModelAdmin):
    list_display = ("order", "name", "code", "default_recipient", "response_days", "is_draft", "is_active")
    list_display_links = ("name",)
    list_editable = ("order", "response_days", "is_draft", "is_active")
    list_filter = ("is_draft", "is_active")
    search_fields = ("name", "code")
    raw_id_fields = ("default_recipient",)
    ordering = ("order", "name")


@admin.register(RequestPackage)
class RequestPackageAdmin(admin.ModelAdmin):
    list_display = ("order", "name", "code", "is_draft", "is_active")
    list_display_links = ("name",)
    list_editable = ("order", "is_draft", "is_active")
    list_filter = ("is_draft", "is_active")
    search_fields = ("name", "code")
    filter_horizontal = ("types",)
    ordering = ("order", "name")


@admin.register(Request)
class RequestAdmin(admin.ModelAdmin):
    list_display = ("title", "case", "recipient_display", "status", "sent_date", "due_date")
    list_filter = ("status", "sent_method")
    search_fields = ("title", "case__service__client__last_name", "recipient_name")
    raw_id_fields = ("case", "request_type", "recipient", "created_by")
