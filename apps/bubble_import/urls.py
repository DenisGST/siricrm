from django.urls import path

from . import views

app_name = "bubble_import"

urlpatterns = [
    path("imports/bubble/", views.panel, name="panel"),
    path("imports/bubble/<str:entity>/", views.panel, name="panel_entity"),
    path("imports/bubble/<str:entity>/table/", views.entity_table, name="entity_table"),
    path("imports/bubble/<str:entity>/fetch/", views.fetch, name="fetch"),
    path("imports/bubble/<str:entity>/apply/", views.apply, name="apply"),
    path("imports/bubble/<str:entity>/bulk-approve/", views.bulk_approve, name="bulk_approve"),
    path("imports/bubble/<str:entity>/select-all/", views.select_all, name="select_all"),
    path("imports/bubble/<str:entity>/<int:pk>/toggle/", views.toggle_approve, name="toggle_approve"),
    path("imports/bubble/<str:entity>/<int:pk>/edit/", views.edit_field, name="edit_field"),

    # Фоновый «Импортировать ВСЁ» + поллинг статуса.
    path("imports/bubble/<str:entity>/full/", views.start_full_import, name="start_full"),
    path("imports/bubble/job/<uuid:job_id>/", views.job_status, name="job_status"),
    path("imports/bubble/job/<uuid:job_id>/cancel/", views.cancel_job, name="cancel_job"),
]
