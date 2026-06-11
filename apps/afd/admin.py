from django.contrib import admin

from .models import (
    DocumentTemplate, ExecutorOrg, GeneratedDocument, IskSection, IskTemplate,
)


class IskSectionInline(admin.TabularInline):
    model = IskSection
    extra = 0
    fields = ("order", "title", "block_type", "is_optional", "include_condition", "is_active")
    ordering = ("order",)


@admin.register(IskTemplate)
class IskTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "is_default", "is_active", "updated_at")
    inlines = [IskSectionInline]


@admin.register(IskSection)
class IskSectionAdmin(admin.ModelAdmin):
    list_display = ("template", "order", "title", "block_type", "is_optional", "is_active")
    list_filter = ("template", "block_type", "is_optional", "is_active")
    ordering = ("template", "order")


@admin.register(ExecutorOrg)
class ExecutorOrgAdmin(admin.ModelAdmin):
    list_display = ("name", "signer_name", "is_default", "is_active")
    list_filter = ("is_active", "is_default")
    search_fields = ("name", "signer_name")


@admin.register(DocumentTemplate)
class DocumentTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "is_active", "updated_at")
    list_filter = ("kind", "is_active")
    search_fields = ("name",)


@admin.register(GeneratedDocument)
class GeneratedDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "client", "service", "created_by", "created_at")
    search_fields = ("title", "client__last_name", "client__first_name")
    raw_id_fields = ("client", "service", "docx_file", "pdf_file", "template")
