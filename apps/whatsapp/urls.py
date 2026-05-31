from django.urls import path

from . import views

app_name = "whatsapp"

urlpatterns = [
    path("webhook/whatsapp/", views.whatsapp_webhook, name="webhook"),
    path("webhook/whatsapp/<str:secret>/", views.whatsapp_webhook, name="webhook_with_secret"),
    path("wa/file/<uuid:file_id>/", views.wa_file_proxy, name="wa_file_proxy"),
]
