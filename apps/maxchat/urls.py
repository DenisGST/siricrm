# apps/maxchat/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("max/webhook/", views.max_webhook, name="max_webhook"),
]
