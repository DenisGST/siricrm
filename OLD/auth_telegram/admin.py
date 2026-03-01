from django.contrib import admin
from .models import TelegramUser


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = ("user", "telegram_id", "username", "is_verified", "last_login", "created_at")
    search_fields = ("user__username", "username", "telegram_id")
    list_filter = ("is_verified",)
    raw_id_fields = ("user",)
    date_hierarchy = "last_login"