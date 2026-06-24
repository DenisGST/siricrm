from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("badge/", views.badge, name="badge"),
    path("panel/", views.panel, name="panel"),
    path("list/", views.panel_list, name="list"),
    path("<int:pk>/<str:action>/", views.respond, name="respond"),
]
