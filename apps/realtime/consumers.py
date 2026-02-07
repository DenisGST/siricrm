from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser

class TelegramChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.client_id = self.scope["url_route"]["kwargs"]["client_id"]
        self.group_name = f"telegram_client_{self.client_id}"

        user = self.scope.get("user")
        if isinstance(user, AnonymousUser) or not user.is_authenticated:
            await self.close()
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def chat_message(self, event):
        # event: {"type": "chat_message", "html": "..."}
        await self.send(text_data=event["html"])


class NotificationsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if isinstance(user, AnonymousUser) or not user.is_authenticated:
            await self.close()
            return

        self.user_group = f"user_notifications_{user.id}"
        await self.channel_layer.group_add(self.user_group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.user_group, self.channel_name)

    async def notify(self, event):
        # event: {"type": "notify", "html": "..."}
        await self.send(text_data=event["html"])
