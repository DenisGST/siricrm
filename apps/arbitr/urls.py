from django.urls import path

from . import views

app_name = "arbitr"

urlpatterns = [
    path("service/<uuid:service_id>/iskotpravlen/", views.mark_iskotpravlen, name="mark_iskotpravlen"),
    path("case/<uuid:case_id>/confirm/", views.confirm_case, name="confirm_case"),
    path("service/<uuid:service_id>/case-block/", views.case_block, name="case_block"),
]
