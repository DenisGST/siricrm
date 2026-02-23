from django.urls import re_path
from .consumers import TelegramChatConsumer, NotificationsConsumer

websocket_urlpatterns = [
    re_path(r"^ws/notifications/$", NotificationsConsumer.as_asgi()),
    re_path(r"^ws/telegram/client/(?P<client_id>[^/]+)/$", TelegramChatConsumer.as_asgi()),
]