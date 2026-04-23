from django.contrib import admin
from .models import Client, ClientEmployee, Message, Service, Region, LegalEntityKind


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
    search_fields = ("content", "client__first_name", "client__last_name", "client__username", "client__phone")  # ✅ расширили поиск
    autocomplete_fields = ("client", "employee",)
    date_hierarchy = "created_at"
    ordering = ("created_at",)

    @admin.display(description="Текст")
    def short_content(self, obj: Message):
        return (obj.content[:50] + "…") if len(obj.content) > 50 else obj.content

@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "get_client_name",
        "get_service_name",
        "agent",
        "status_service",
        "status_callcenter",
        "status_consultant",
        "status_sbor",
        "status_bfl",
    )
    list_filter = (
        "name",
        "status_service",
        "status_callcenter",
        "status_consultant",
        "status_sbor",
        "status_bfl",
        "payment_procedure",
        "payment_as",
        "is_active",
    )
    search_fields = (
        "client__first_name",
        "client__last_name",
        "client__username",
        "numb_dogovor",
    )

    def get_client_name(self, obj):
        return f"{obj.client.first_name} {obj.client.last_name}"
    get_client_name.short_description = "Клиент"

    def get_service_name(self, obj):
        return obj.get_name_display()
    get_service_name.short_description = "Услуга"

@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ('number', 'name', 'court_name')
    search_fields = ('number', 'name', 'court_name', 'court_address')
    ordering = ('number',)
