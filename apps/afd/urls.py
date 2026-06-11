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

    # Заявление о банкротстве (иск)
    path("isk/section/new/", views.isk_section_edit, name="isk_section_create"),
    path("isk/section/<uuid:pk>/", views.isk_section_edit, name="isk_section_edit"),
    path("isk/section/<uuid:pk>/delete/", views.isk_section_delete, name="isk_section_delete"),
    path("isk/section/<uuid:pk>/move/<str:direction>/", views.isk_section_move, name="isk_section_move"),
    path("isk/<uuid:service_id>/review/", views.isk_review, name="isk_review"),
    path("isk/<uuid:service_id>/creditors/", views.isk_creditors, name="isk_creditors"),
    path("isk/<uuid:service_id>/generate/", views.isk_generate, name="isk_generate"),
]
