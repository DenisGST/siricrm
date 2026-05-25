from django.contrib import admin
from .models import (
    Client, ClientEmployee, ClientPhone, Message, Service, Region,
    LegalEntityKind, ClientEvent, ServiceName, PaymentProcedure,
    ServiceCommonStatus, ServiceEmployeeStatus, ServiceTag,
    ServiceEmployeeState, ServiceTagAssignment, ServiceLog,
)


@admin.register(ClientPhone)
class ClientPhoneAdmin(admin.ModelAdmin):
    list_display = ("client", "phone", "purpose", "is_active", "created_at")
    list_filter = ("purpose", "is_active")
    search_fields = ("phone", "client__last_name", "client__first_name")
    autocomplete_fields = ("client",)


@admin.register(LegalEntityKind)
class LegalEntityKindAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name")
    search_fields = ("name", "short_name")
    ordering = ("name",)


class ClientEmployeeInline(admin.TabularInline):
    model = ClientEmployee
    extra = 0


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = (
        "first_name",
        "last_name",
        "patronymic",
        "username",
        "telegram_id",
        "status",
        "last_message_at",
        "contacts_confirmed",
    )
    list_filter = ("status",)
    search_fields = (
        "first_name",
        "last_name",
        "username",
        "phone",
        "phones__phone",
        "email",
        "telegram_id",
        "contacts_confirmed",
    )
    inlines = [ClientEmployeeInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "client",
        "employee",
        "direction",
        "message_type",
        "channel",
        "short_content",
        "is_read",
        "created_at",
        "telegram_date",
        "raw_payload",
    )
    list_filter = ("direction", "message_type", "is_read", "client")  # ✅ добавили "client"
    search_fields = (
        "content", "client__first_name", "client__last_name",
        "client__username", "client__phone", "client__phones__phone",
    )
    autocomplete_fields = ("client", "employee",)
    date_hierarchy = "created_at"
    ordering = ("created_at",)

    @admin.display(description="Текст")
    def short_content(self, obj: Message):
        return (obj.content[:50] + "…") if len(obj.content) > 50 else obj.content

class ServiceEmployeeStateInline(admin.TabularInline):
    model = ServiceEmployeeState
    extra = 0


class ServiceTagAssignmentInline(admin.TabularInline):
    model = ServiceTagAssignment
    extra = 0


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "numb_dogovor", "get_client_name", "name", "region",
        "agent", "common_status", "is_active",
    )
    list_filter = ("name", "region", "common_status", "payment_procedure", "is_active")
    search_fields = (
        "client__first_name", "client__last_name", "client__username",
        "numb_dogovor",
    )
    inlines = [ServiceEmployeeStateInline, ServiceTagAssignmentInline]

    def get_client_name(self, obj):
        return f"{obj.client.first_name} {obj.client.last_name}"
    get_client_name.short_description = "Клиент"


@admin.register(ServiceName)
class ServiceNameAdmin(admin.ModelAdmin):
    list_display = ("short_name", "full_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("full_name", "short_name")
    filter_horizontal = ("departments",)


@admin.register(PaymentProcedure)
class PaymentProcedureAdmin(admin.ModelAdmin):
    list_display = ("short_name", "full_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("full_name", "short_name")


@admin.register(ServiceCommonStatus)
class ServiceCommonStatusAdmin(admin.ModelAdmin):
    list_display = ("service_name", "name", "order", "is_active")
    list_filter = ("service_name", "is_active")
    search_fields = ("name",)
    ordering = ("service_name", "order")


@admin.register(ServiceEmployeeStatus)
class ServiceEmployeeStatusAdmin(admin.ModelAdmin):
    list_display = ("employee", "common_status", "name", "order", "is_active")
    list_filter = ("employee", "common_status__service_name", "is_active")
    search_fields = ("name",)


@admin.register(ServiceTag)
class ServiceTagAdmin(admin.ModelAdmin):
    list_display = ("employee", "name", "color", "is_active")
    list_filter = ("employee", "is_active")
    search_fields = ("name",)


@admin.register(ServiceLog)
class ServiceLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "service", "employee", "action")
    list_filter = ("action",)
    search_fields = ("service__numb_dogovor", "comment")
    date_hierarchy = "created_at"

@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ('number', 'name', 'court_name')
    search_fields = ('number', 'name', 'court_name', 'court_address')
    ordering = ('number',)


@admin.register(ClientEvent)
class ClientEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "client", "event_type", "employee", "description")
    list_filter = ("event_type",)
    search_fields = ("client__last_name", "client__first_name", "description")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
