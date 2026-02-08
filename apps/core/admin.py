from django.contrib import admin
from .models import Department, Employee, EmployeeLog

# Register your models here.

@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "user_full_name",
        "department",
        "is_active",
        "is_online",
        "last_seen",
    )
    list_filter = ("is_active", "is_online", "department")
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
    )
    autocomplete_fields = ("user", "department")
    ordering = ("-last_seen",)

    @admin.display(description="Сотрудник")
    def user_full_name(self, obj: Employee):
        return obj.user.get_full_name() or obj.user.username

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "manager", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    autocomplete_fields = ("manager",)