from django.urls import path

from . import views

app_name = "wiki"

urlpatterns = [
    path("", views.index, name="index"),
    path("search/", views.search_results, name="search"),
    path("preview/", views.preview, name="preview"),
    path("new/", views.article_new, name="article_new"),
    path("a/<slug:slug>/", views.article, name="article"),
    path("a/<slug:slug>/edit/", views.article_edit, name="article_edit"),
    path("a/<slug:slug>/delete/", views.article_delete, name="article_delete"),
    path("a/<slug:slug>/move/", views.article_move, name="article_move"),
]
