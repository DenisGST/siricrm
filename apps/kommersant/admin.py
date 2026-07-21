from django.contrib import admin

from .models import KommersantAttachment, KommersantMessageType, KommersantPublication


@admin.register(KommersantMessageType)
class KommersantMessageTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "blank_checkbox", "order", "is_active", "is_draft")
    list_filter = ("is_active", "is_draft", "blank_checkbox")
    search_fields = ("code", "name")
    ordering = ("order", "name")


class KommersantAttachmentInline(admin.TabularInline):
    model = KommersantAttachment
    extra = 0
    autocomplete_fields = ("stored_file",)


@admin.register(KommersantPublication)
class KommersantPublicationAdmin(admin.ModelAdmin):
    list_display = ("__str__", "case", "status", "sent_at",
                    "invoice_number", "is_paid", "publication_date")
    list_filter = ("status", "is_paid")
    search_fields = ("title", "invoice_number", "announcement_number", "sent_message_id")
    date_hierarchy = "created_at"
    inlines = [KommersantAttachmentInline]
    readonly_fields = ("sent_message_id", "invoice_message_id", "generated_at")
