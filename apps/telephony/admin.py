from django.contrib import admin

from .models import Call, CallGroup, CallListen, MissedCall


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
    list_display = ("listened_at", "employee", "call", "missed_call", "ip")
    list_filter = ("listened_at",)
    raw_id_fields = ("call", "missed_call", "employee")


@admin.register(CallGroup)
class CallGroupAdmin(admin.ModelAdmin):
    """Кому уходят уведомления о пропущенных.

    🛑 Отдел у групп после выкатки пустой (названия отделов на dev и на проде
    свои, автопривязка увела бы уведомления не тем людям) — выбрать руками,
    иначе пропущенные идут только руководству.
    """

    list_display = ("name", "code", "extensions", "department",
                    "notify_department", "notify_management", "is_active")
    list_filter = ("is_active", "notify_department", "notify_management")
    search_fields = ("code", "name", "extensions")
    filter_horizontal = ("subscribers",)


@admin.register(MissedCall)
class MissedCallAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "kind", "phone", "client", "group",
                    "extension", "status", "assignee", "notified_at")
    list_filter = ("status", "kind", "group", "occurred_at")
    search_fields = ("phone", "raw_phone", "linkedid", "uniqueid")
    date_hierarchy = "occurred_at"
    raw_id_fields = ("client", "assignee", "handled_by", "recording", "call",
                     "closed_by_call")
    readonly_fields = ("created_at", "updated_at")
