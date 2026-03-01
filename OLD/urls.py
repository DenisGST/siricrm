from django.urls import path
from . import views

urlpatterns = [
    path('', views.telegram_webhook, name='telegram_webhook'),
]