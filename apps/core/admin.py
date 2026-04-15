from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from .models import Department, Employee, EmployeeLog, MenuItem, Widget, DashboardConfig


class EmployeeInline(admin.StackedInline):
    model = Employee
    can_delete = False
    verbose_name = "Сотрудник"
    verbose_name_plural = "Сотрудник"
    fieldsets = (
        ("Персональная информация", {
            "fields": ("patronymic", "phone_mobile", "phone_internal"),
        }),
        ("Работа", {
            "fields": ("department", "role", "dashboard_config", "has_messenger_access", "is_active"),
        }),
        ("Статус", {
            "fields": ("is_online", "joined_at", "dismiss_at"),
        }),
    )
    readonly_fields = ("joined_at",)


admin.site.unregister(User)

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    inlines = [EmployeeInline]


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "user_full_name",
        "department",
        "is_active",
        "is_online",
    )
    list_filter = ("is_active", "is_online", "department")
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
    )
    autocomplete_fields = ("user", "department")

    @admin.display(description="Сотрудник")
    def user_full_name(self, obj: Employee):
        return obj.user.get_full_name() or obj.user.username

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "manager", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    autocomplete_fields = ("manager",)

@admin.register(EmployeeLog)
class EmployeeLogAdmin(admin.ModelAdmin):
    list_display = ("employee", "action", "client", "timestamp", "ip_address")
    list_filter = ("action", "timestamp")
    search_fields = ("description", "ip_address", "user_agent")
    autocomplete_fields = ("employee", "client", "message")
    date_hierarchy = "timestamp"
    ordering = ("-timestamp",)


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ("name", "icon", "url", "section", "order", "use_htmx", "is_active")
    list_filter = ("section", "is_active", "requires_superuser")
    list_editable = ("order", "is_active")
    ordering = ("section", "order")


@admin.register(Widget)
class WidgetAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "widget_type", "order", "is_active")
    list_filter = ("widget_type", "is_active")
    list_editable = ("order", "is_active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(DashboardConfig)
class DashboardConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "is_default", "is_active")
    list_filter = ("is_active", "is_default")
    filter_horizontal = ("menu_items", "widgets")