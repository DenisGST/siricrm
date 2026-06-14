from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from django.views.static import serve
from django.conf import settings

from apps.core.health import health_check

from apps.crm.api import (
    ClientViewSet,
    MessageViewSet,
    
)
from apps.core.api import (
    DepartmentViewSet,
    EmployeeViewSet,
    EmployeeLogViewSet,
    stats_view,
)

# API Router
api_router = DefaultRouter()
api_router.register(r'departments', DepartmentViewSet, basename='department')
api_router.register(r'employees', EmployeeViewSet, basename='employee')
api_router.register(r'clients', ClientViewSet, basename='client')
api_router.register(r'messages', MessageViewSet, basename='message')
api_router.register(r'logs', EmployeeLogViewSet, basename='log')

urlpatterns = [
    # Health check (для мониторинга / nginx)
    path("health/", health_check, name="health"),

    # Admin
    path('admin/', admin.site.urls),

    path("api/stats/", stats_view, name="api-stats"),
    
    
    # API
    path('api/', include(api_router.urls)),
    path("api/", include("apps.maxchat.urls")),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/swagger/', SpectacularSwaggerView.as_view(url_name='schema')),
    
    # Обычный Django login/logout по /accounts/login/
    path("accounts/", include("django.contrib.auth.urls")),
    
    # CRM views
    path('', include('apps.crm.urls')),

    # files view
    path("files/", include(("apps.files.urls", "files"))),
    
    path("", include("apps.core.urls")),
    path("consultations/", include("apps.consultations.urls", namespace="consultations")),
    path("questionnaire/", include("apps.questionnaire.urls", namespace="questionnaire")),
    path("devops/", include("apps.devops.urls", namespace="devops")),
    path("finance/", include("apps.finance.urls", namespace="finance")),
    path("", include("apps.whatsapp.urls", namespace="whatsapp")),
    path("", include("apps.bubble_import.urls", namespace="bubble_import")),
    path("telegram/", include("apps.telegram.urls", namespace="telegram")),
    path("arbitr/", include("apps.arbitr.urls", namespace="arbitr")),
    path("afd/", include("apps.afd.urls", namespace="afd")),
    path("scans/", include("apps.scans.urls", namespace="scans")),
    path("notifications/", include("apps.notifications.urls", namespace="notifications")),
    path("robots.txt", serve, {"document_root": settings.STATIC_ROOT, "path": "robots.txt"}),
]
