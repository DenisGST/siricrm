from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('monitoring/', views.monitoring_dashboard, name='monitoring_dashboard'),
    path('monitoring/api/', views.monitoring_api, name='monitoring_api'),
]
