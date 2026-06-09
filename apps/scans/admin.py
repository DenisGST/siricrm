from django.contrib import admin

from .models import IncomingScan


@admin.register(IncomingScan)
class IncomingScanAdmin(admin.ModelAdmin):
    list_display = ("filename", "status", "source", "client", "received_at", "handled_by")
    list_filter = ("status", "source")
    search_fields = ("filename", "source_meta")
    readonly_fields = ("received_at", "handled_at")
    autocomplete_fields = ("client",)
    list_select_related = ("client", "handled_by")
