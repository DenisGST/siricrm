from django.urls import path
from . import views

urlpatterns = [
    path("<uuid:file_id>/", views.download_file, name="file_download"),
]