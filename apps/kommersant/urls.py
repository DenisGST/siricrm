from django.urls import path

from . import views

app_name = "kommersant"

urlpatterns = [
    path("service/<uuid:service_id>/", views.subtab, name="subtab"),

    # Текст сообщения
    path("service/<uuid:service_id>/add/", views.publication_add, name="publication_add"),
    path("service/<uuid:service_id>/gen-text/", views.publication_gen_text,
         name="publication_gen_text"),
    path("service/<uuid:service_id>/save/", views.publication_save, name="publication_save"),
    path("service/<uuid:service_id>/<uuid:pub_id>/edit/", views.publication_edit,
         name="publication_edit"),
    path("service/<uuid:service_id>/<uuid:pub_id>/text/", views.publication_text,
         name="publication_text"),
    path("service/<uuid:service_id>/<uuid:pub_id>/delete/", views.publication_delete,
         name="publication_delete"),
    path("service/<uuid:service_id>/<uuid:pub_id>/cancel/", views.publication_cancel,
         name="publication_cancel"),

    # Заявка и вложения
    path("service/<uuid:service_id>/<uuid:pub_id>/blank/", views.blank_generate,
         name="blank_generate"),
    path("service/<uuid:service_id>/<uuid:pub_id>/send-modal/", views.send_modal,
         name="send_modal"),
    path("service/<uuid:service_id>/<uuid:pub_id>/attach/", views.attachment_add,
         name="attachment_add"),
    path("service/<uuid:service_id>/<uuid:pub_id>/attach/<uuid:att_id>/delete/",
         views.attachment_delete, name="attachment_delete"),

    # Отправка и счёт
    path("service/<uuid:service_id>/<uuid:pub_id>/send/", views.send_request,
         name="send_request"),
    path("service/<uuid:service_id>/<uuid:pub_id>/fetch-invoice/", views.fetch_invoice,
         name="fetch_invoice"),
    path("service/<uuid:service_id>/<uuid:pub_id>/invoice/", views.invoice_modal,
         name="invoice_modal"),
    path("service/<uuid:service_id>/<uuid:pub_id>/invoice/save/", views.invoice_save,
         name="invoice_save"),
    path("service/<uuid:service_id>/<uuid:pub_id>/published/", views.published_modal,
         name="published_modal"),
    path("service/<uuid:service_id>/<uuid:pub_id>/published/save/", views.published_save,
         name="published_save"),
]
