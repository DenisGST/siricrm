from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # Лента чата конкретного клиента
     re_path(r"^ws/telegram/(?P<client_id>[^/]+)/$", consumers.TelegramChatConsumer.as_asgi()),
    # Глобальные уведомления/тосты для сотрудников
    re_path(r"^ws/notifications/$", consumers.NotificationsConsumer.as_asgi()),
]