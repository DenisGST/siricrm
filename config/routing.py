# siricrm/routing.py
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import apps.realtime.routing

application = ProtocolTypeRouter({
    # HTTP Django обрабатывает сам (через Django's ASGIHandler, если он у тебя подключен в asgi.py)
    "websocket": AuthMiddlewareStack(
        URLRouter(apps.realtime.routing.websocket_urlpatterns)
    ),
})