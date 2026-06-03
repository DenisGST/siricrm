from django.contrib import admin

from .models import DocumentTemplate, ExecutorOrg, GeneratedDocument


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
