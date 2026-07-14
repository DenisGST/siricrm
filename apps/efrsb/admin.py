from django.contrib import admin

from .models import (
    EfrsbBankruptLink,
    EfrsbMessageSubtype,
    EfrsbMessageType,
    EfrsbPublication,
    EfrsbPublicationFile,
)


class EfrsbMessageSubtypeInline(admin.TabularInline):
    model = EfrsbMessageSubtype
    extra = 0
    fields = ("code", "name", "applicable_kinds", "is_bfl", "is_active", "is_draft", "order")


@admin.register(EfrsbMessageType)
class EfrsbMessageTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "api_type", "is_bfl", "is_active", "is_draft", "order")
    list_filter = ("is_active", "is_bfl", "is_draft")
    search_fields = ("name", "code", "api_type")
    ordering = ("order", "name")
    inlines = [EfrsbMessageSubtypeInline]


@admin.register(EfrsbMessageSubtype)
class EfrsbMessageSubtypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "message_type", "is_bfl", "is_active", "is_draft")
    list_filter = ("is_active", "is_bfl", "is_draft", "message_type")
    search_fields = ("name", "code")


@admin.register(EfrsbBankruptLink)
class EfrsbBankruptLinkAdmin(admin.ModelAdmin):
    list_display = ("case", "bankrupt_guid", "match_method", "match_confidence", "resolved_at")
    search_fields = ("bankrupt_guid",)
    raw_id_fields = ("case",)


class EfrsbPublicationFileInline(admin.TabularInline):
    model = EfrsbPublicationFile
    extra = 0
    raw_id_fields = ("stored_file",)


@admin.register(EfrsbPublication)
class EfrsbPublicationAdmin(admin.ModelAdmin):
    list_display = (
        "title", "kind", "origin", "status", "date_publish",
        "fedresurs_number", "has_violation", "is_annulled", "is_locked",
    )
    list_filter = ("kind", "origin", "status", "has_violation", "is_annulled", "is_locked")
    search_fields = ("title", "fedresurs_guid", "fedresurs_number", "bankrupt_guid")
    raw_id_fields = ("case", "procedure", "message_type", "content_docx", "content_pdf", "created_by")
    date_hierarchy = "date_publish"
    inlines = [EfrsbPublicationFileInline]
