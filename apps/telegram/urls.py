from django.urls import path

from . import leads_bot

app_name = "telegram"

urlpatterns = [
    path("leads-webhook/<str:secret>/", leads_bot.leads_webhook, name="leads_webhook"),
]
