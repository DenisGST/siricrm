from django.urls import path
from . import views

app_name = "consultations"

urlpatterns = [
    path("",                       views.schedule,               name="schedule"),
    path("history/",               views.history,                name="history"),
    path("book/",                  views.book,                   name="book"),
    path("book-modal/",            views.book_modal,             name="book_modal"),
    path("client-search/",         views.client_search,          name="client_search"),
    path("<uuid:pk>/result/",      views.result_modal,           name="result_modal"),
    path("<uuid:pk>/set-result/",  views.set_result,             name="set_result"),
    path("<uuid:pk>/move-modal/",   views.move_modal,             name="move_modal"),
    path("<uuid:pk>/move-confirm/", views.move_confirm,           name="move_confirm"),
    path("<uuid:pk>/edit/",         views.edit_modal,             name="edit"),
    path("results/",               views.result_reference,       name="result_reference"),
    path("results/add/",           views.result_reference_form,  name="result_add"),
    path("results/<uuid:pk>/edit/",views.result_reference_form,  name="result_edit"),
    path("results/<uuid:pk>/del/", views.result_reference_delete,name="result_delete"),
]
