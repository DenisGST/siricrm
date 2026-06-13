from django.urls import path

from . import views

app_name = "arbitr"

urlpatterns = [
    # UI-эндпоинты блока «Арбитражное дело» в карточке услуги (старое)
    path("service/<uuid:service_id>/iskotpravlen/", views.mark_iskotpravlen, name="mark_iskotpravlen"),
    path("case/<uuid:case_id>/confirm/", views.confirm_case, name="confirm_case"),
    path("service/<uuid:service_id>/case-block/", views.case_block, name="case_block"),

    # Сервисная страница мониторинга
    path("", views.dashboard, name="dashboard"),
    path("case/<uuid:case_id>/", views.case_detail, name="case_detail"),
    path("case/<uuid:case_id>/run/", views.case_run, name="case_run"),
    path("case/<uuid:case_id>/toggle-pause/", views.case_toggle_pause, name="case_toggle_pause"),
    path("case/<uuid:case_id>/card/", views.case_card_partial, name="case_card"),
    path("case/<uuid:case_id>/log/", views.case_log_partial, name="case_log"),
    path(
        "case/<uuid:case_id>/confirm-hit/<int:hit_index>/",
        views.case_confirm_hit, name="case_confirm_hit",
    ),
]
