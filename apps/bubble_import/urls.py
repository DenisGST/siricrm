from django.urls import path

from . import views

app_name = "bubble_import"

urlpatterns = [
    path("imports/bubble/", views.panel, name="panel"),
    path("imports/bubble/clients/", views.clients_table, name="clients_table"),
    path("imports/bubble/fetch/", views.fetch, name="fetch"),
    path("imports/bubble/apply/", views.apply, name="apply"),
    path("imports/bubble/bulk-approve/", views.bulk_approve, name="bulk_approve"),
    path("imports/bubble/select-all/", views.select_all, name="select_all"),
    path("imports/bubble/<int:pk>/toggle/", views.toggle_approve, name="toggle_approve"),
    path("imports/bubble/<int:pk>/edit/", views.edit_field, name="edit_field"),
]
