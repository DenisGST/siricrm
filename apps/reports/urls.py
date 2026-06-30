from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.panel, name="panel"),
    path("tab/sales/", views.tab_sales, name="tab_sales"),
    path("budget/calculate/", views.budget_calculate, name="budget_calculate"),
    path("budget/entry/<uuid:payment_id>/", views.budget_entry_save, name="budget_entry_save"),
]
