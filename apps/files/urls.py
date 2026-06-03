from django.urls import path
from . import views

app_name = "files"

urlpatterns = [
    # Файловый менеджер
    path("client/<uuid:client_pk>/",           views.file_manager,         name="manager"),
    path("client/<uuid:client_pk>/search/",    views.file_search,          name="search"),
    path("folder/<uuid:folder_pk>/",           views.folder_contents,      name="folder_contents"),
    path("folder/<uuid:folder_pk>/upload/",    views.file_upload,          name="file_upload"),
    path("folder/<uuid:parent_pk>/mkdir/",     views.folder_create,        name="folder_create"),
    path("folder/<uuid:folder_pk>/rename/",    views.folder_rename,        name="folder_rename"),
    path("folder/<uuid:folder_pk>/delete/",    views.folder_delete,        name="folder_delete"),
    path("file/<uuid:file_pk>/download/",      views.file_download,        name="file_download"),
    path("file/<uuid:file_pk>/delete/",        views.file_delete,          name="file_delete"),
    path("file/<uuid:file_pk>/move/",          views.file_move,            name="file_move"),
    path("file/<uuid:file_pk>/preview/",      views.file_preview,         name="file_preview"),
    # Скачать StoredFile (используется в чате)
    path("<uuid:file_id>/",                    views.download_stored_file, name="stored_download"),
]
