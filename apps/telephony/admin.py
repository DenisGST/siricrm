from django.contrib import admin

from .models import Call, CallListen


@admin.register(Call)
class CallAdmin(admin.ModelAdmin):
    list_display = ("started_at", "direction", "src", "dst", "extension",
                    "employee", "client", "billsec", "disposition", "recording")
    list_filter = ("direction", "disposition", "started_at")
    search_fields = ("uniqueid", "src", "dst", "clid", "counterparty_phone")
    date_hierarchy = "started_at"
    raw_id_fields = ("employee", "client", "recording")
    readonly_fields = ("created_at", "updated_at")


@admin.register(CallListen)
class CallListenAdmin(admin.ModelAdmin):
    list_display = ("listened_at", "employee", "call", "ip")
    list_filter = ("listened_at",)
    raw_id_fields = ("call", "employee")
