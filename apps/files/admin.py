from django.contrib import admin

from .models import StoredFile


@admin.register(StoredFile)
class StoredFileAdmin(admin.ModelAdmin):
    list_display = ("filename", "bucket", "key", "created_at", "id")
    search_fields = ("filename", "bucket", "key")
    list_filter = ("bucket", "created_at")
    ordering = ("-created_at",)
