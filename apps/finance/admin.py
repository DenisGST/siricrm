from django.contrib import admin

from . import models


@admin.register(models.ExpenseType)
class ExpenseTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "service_name", "is_active")
    list_filter = ("service_name", "is_active")
    search_fields = ("name",)


@admin.register(models.IncomeType)
class IncomeTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "service_name", "is_active")
    list_filter = ("service_name", "is_active")
    search_fields = ("name",)


@admin.register(models.IncomingAccount)
class IncomingAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "account_type", "is_active")
    list_filter = ("account_type", "is_active")
    search_fields = ("name",)


@admin.register(models.OutgoingAccount)
class OutgoingAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "account_type", "is_active")
    list_filter = ("account_type", "is_active")
    search_fields = ("name",)


@admin.register(models.Charge)
class ChargeAdmin(admin.ModelAdmin):
    list_display = ("client", "title", "due_date", "amount", "status")
    list_filter = ("status",)
    search_fields = ("title", "client__last_name", "client__first_name")
    raw_id_fields = ("client", "service", "created_by", "updated_by")
    date_hierarchy = "due_date"


@admin.register(models.Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "payment_date", "direction", "client",
        "amount_in", "amount_out", "payment_form",
    )
    list_filter = ("direction", "payment_form")
    search_fields = ("client__last_name", "client__first_name", "comments")
    raw_id_fields = (
        "client", "service", "charge",
        "expense_type", "income_type",
        "incoming_account", "outgoing_account",
        "created_by", "updated_by",
    )
    date_hierarchy = "payment_date"
