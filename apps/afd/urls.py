from django.urls import path

from . import views

app_name = "afd"

urlpatterns = [
    path("", views.panel, name="panel"),

    path("executors/new/", views.executor_edit, name="executor_create"),
    path("executors/<uuid:pk>/", views.executor_edit, name="executor_edit"),
    path("executors/<uuid:pk>/delete/", views.executor_delete, name="executor_delete"),

    path("templates/new/", views.template_edit, name="template_create"),
    path("templates/<uuid:pk>/", views.template_edit, name="template_edit"),
    path("templates/<uuid:pk>/delete/", views.template_delete, name="template_delete"),

    path("contract/<uuid:service_id>/check/", views.contract_check, name="contract_check"),
    path("contract/<uuid:service_id>/generate/", views.contract_generate, name="contract_generate"),
    path("contract/sent/<uuid:gen_id>/<str:channel>/", views.contract_send, name="contract_send"),
]
