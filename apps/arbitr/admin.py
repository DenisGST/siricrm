from django.contrib import admin

from .models import ArbitrCase, ArbitrEvent, ArbitrAttachment, ArbitrCheckLog


@admin.register(ArbitrCase)
class ArbitrCaseAdmin(admin.ModelAdmin):
    list_display = (
        "case_number", "status", "service", "started_by",
        "last_check_at", "last_check_ok", "created_at",
    )
    list_filter = ("status", "last_check_ok")
    search_fields = ("case_number", "kad_url", "service__client__last_name")
    autocomplete_fields = ("service", "started_by")


class ArbitrAttachmentInline(admin.TabularInline):
    model = ArbitrAttachment
    extra = 0


@admin.register(ArbitrEvent)
class ArbitrEventAdmin(admin.ModelAdmin):
    list_display = ("event_date", "kind", "title", "case", "parsed_at")
    list_filter = ("kind",)
    search_fields = ("title", "description", "kad_event_id")
    autocomplete_fields = ("case",)
    inlines = [ArbitrAttachmentInline]


@admin.register(ArbitrCheckLog)
class ArbitrCheckLogAdmin(admin.ModelAdmin):
    list_display = ("ts", "case", "state", "duration_ms")
    list_filter = ("state",)
    search_fields = ("case__case_number", "notes")
