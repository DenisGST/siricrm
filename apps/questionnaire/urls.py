from django.urls import path
from . import views

app_name = "questionnaire"

urlpatterns = [
    # Справочник шаблонов
    path("",                                       views.template_list,   name="template_list"),
    path("create/",                                views.template_create, name="template_create"),
    path("<uuid:pk>/",                             views.template_detail, name="template_detail"),
    path("<uuid:pk>/toggle/",                      views.template_toggle, name="template_toggle"),
    path("<uuid:tmpl_pk>/pages/add/",              views.page_add,        name="page_add"),
    path("pages/<uuid:pk>/delete/",               views.page_delete,     name="page_delete"),
    path("pages/<uuid:page_pk>/questions/add/",   views.question_form,   name="question_add"),
    path("pages/<uuid:page_pk>/questions/<uuid:q_pk>/edit/", views.question_form, name="question_edit"),
    path("questions/<uuid:pk>/delete/",           views.question_delete, name="question_delete"),
    path("ref-search/",                   views.ref_search,      name="ref_search"),
    # Список анкет
    path("client/<uuid:client_pk>/responses/",    views.client_responses,  name="client_responses"),
    path("service/<uuid:service_pk>/responses/",  views.service_responses, name="service_responses"),
    path("response/<uuid:pk>/delete/",            views.response_delete,   name="response_delete"),
    # Квиз
    path("start/<uuid:service_pk>/",              views.quiz_start,        name="quiz_start"),
    path("response/<uuid:pk>/page/<int:page_num>/",      views.quiz_page,       name="quiz_page"),
    path("response/<uuid:pk>/page/<int:page_num>/save/", views.quiz_save_page,  name="quiz_save_page"),
    path("response/<uuid:pk>/complete/",          views.quiz_complete,        name="quiz_complete"),
    path("quick-add-client/",                     views.quick_add_client,     name="quick_add_client"),
    path("response/<uuid:pk>/pdf/",               views.download_pdf,         name="download_pdf"),
]
