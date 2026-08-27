from django.contrib import admin

from .models import CallCenterCard, CallCenterColumn


@admin.register(CallCenterColumn)
class CallCenterColumnAdmin(admin.ModelAdmin):
    list_display = ("name", "order", "color", "wip_limit", "is_default", "is_active")
    list_editable = ("order", "is_active")


@admin.register(CallCenterCard)
class CallCenterCardAdmin(admin.ModelAdmin):
    list_display = ("client", "column", "moved_at", "moved_by")
    list_filter = ("column",)
    raw_id_fields = ("client",)
