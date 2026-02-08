from django.contrib import admin
from .models import Client, Message, Service


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
    list_filter = ("status",)
    autocomplete_fields = ("employees",)  
    filter_horizontal = ("employees",)    


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "client",
        "employee",
        "direction",
        "message_type",
        "short_content",
        "is_read",
        "created_at",
        
    )
    list_filter = ("direction", "message_type", "is_read",)
    search_fields = ("content",)
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