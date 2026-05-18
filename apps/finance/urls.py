from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    # Справочники
    path("references/expense-types/", views.references_expense_types, name="references_expense_types"),
    path("references/expense-type/add/", views.reference_expense_type_edit, name="reference_expense_type_add"),
    path("references/expense-type/<uuid:pk>/", views.reference_expense_type_edit, name="reference_expense_type_edit"),
    path("references/expense-type/<uuid:pk>/delete/", views.reference_expense_type_delete, name="reference_expense_type_delete"),

    path("references/income-types/", views.references_income_types, name="references_income_types"),
    path("references/income-type/add/", views.reference_income_type_edit, name="reference_income_type_add"),
    path("references/income-type/<uuid:pk>/", views.reference_income_type_edit, name="reference_income_type_edit"),
    path("references/income-type/<uuid:pk>/delete/", views.reference_income_type_delete, name="reference_income_type_delete"),

    path("references/incoming-accounts/", views.references_incoming_accounts, name="references_incoming_accounts"),
    path("references/incoming-account/add/", views.reference_incoming_account_edit, name="reference_incoming_account_add"),
    path("references/incoming-account/<uuid:pk>/", views.reference_incoming_account_edit, name="reference_incoming_account_edit"),
    path("references/incoming-account/<uuid:pk>/delete/", views.reference_incoming_account_delete, name="reference_incoming_account_delete"),

    path("references/outgoing-accounts/", views.references_outgoing_accounts, name="references_outgoing_accounts"),
    path("references/outgoing-account/add/", views.reference_outgoing_account_edit, name="reference_outgoing_account_add"),
    path("references/outgoing-account/<uuid:pk>/", views.reference_outgoing_account_edit, name="reference_outgoing_account_edit"),
    path("references/outgoing-account/<uuid:pk>/delete/", views.reference_outgoing_account_delete, name="reference_outgoing_account_delete"),

    # График платежей по услуге
    path("service/<uuid:service_id>/schedule/", views.payment_schedule_modal, name="payment_schedule_modal"),
    path("service/<uuid:service_id>/charge/add/", views.charge_edit, name="charge_add"),
    path("service/<uuid:service_id>/charge/<uuid:charge_id>/edit/", views.charge_edit, name="charge_edit"),
    path("service/<uuid:service_id>/charge/<uuid:charge_id>/delete/", views.charge_delete, name="charge_delete"),

    # Финансы по клиенту
    path("client/<uuid:client_id>/finance/", views.finance_modal, name="finance_modal"),
    path("client/<uuid:client_id>/payment/<str:direction>/add/", views.payment_form_view, name="payment_add"),
    path("client/<uuid:client_id>/payment/<uuid:payment_id>/", views.payment_form_view, name="payment_edit"),
    path("client/<uuid:client_id>/payment/<uuid:payment_id>/delete/", views.payment_delete, name="payment_delete"),
]
