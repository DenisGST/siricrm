from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.crm.api import (
    DepartmentViewSet,
    OperatorViewSet,
    ClientViewSet,
    MessageViewSet,
    OperatorLogViewSet,
    stats_view,
)

# API Router
api_router = DefaultRouter()
api_router.register(r'departments', DepartmentViewSet, basename='department')
api_router.register(r'operators', OperatorViewSet, basename='operator')
api_router.register(r'clients', ClientViewSet, basename='client')
api_router.register(r'messages', MessageViewSet, basename='message')
api_router.register(r'logs', OperatorLogViewSet, basename='log')

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),

    path("api/stats/", stats_view, name="api-stats"),
    
    
    # API
    path('api/', include(api_router.urls)),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/swagger/', SpectacularSwaggerView.as_view(url_name='schema')),
    
    # Telegram webhook
    path('api/telegram/webhook/', include('apps.telegram.urls')),
    
    # Auth
    path('api/auth/', include('apps.auth_telegram.urls')),

     # Обычный Django login/logout по /accounts/login/
    path("accounts/", include("django.contrib.auth.urls")),
    
    # CRM views
    path('', include('apps.crm.urls')),

    # files view
    path("files/", include("apps.files.urls")),
]
