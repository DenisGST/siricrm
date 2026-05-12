from django.contrib import admin

from .models import DevopsAction, DevopsAgentJob, Environment


@admin.register(Environment)
class EnvironmentAdmin(admin.ModelAdmin):
    list_display = ("name", "base_url", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "base_url")


@admin.register(DevopsAction)
class DevopsActionAdmin(admin.ModelAdmin):
    list_display = ("started_at", "action_type", "environment", "status", "started_by")
    list_filter = ("action_type", "status", "environment")
    readonly_fields = ("id", "started_at", "finished_at", "output", "params", "remote_job_id")


@admin.register(DevopsAgentJob)
class DevopsAgentJobAdmin(admin.ModelAdmin):
    list_display = ("started_at", "action_type", "status", "finished_at")
    list_filter = ("action_type", "status")
    readonly_fields = ("id", "started_at", "finished_at", "output", "result", "params")
