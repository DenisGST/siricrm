# /var/www/projects/siricrm/apps/realtime/consumers.py
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from asgiref.sync import sync_to_async
from django.contrib.auth.models import AnonymousUser

from apps.core.models import Employee
from apps.crm.models import Client

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
        # Входящие сообщения — raw HTML для OOB-вставки через HTMX-WS
        await self.send(text_data=event["html"])

    async def chat_message_status(self, event):
        # Обновление статуса исходящего — JSON для JS-обработчика
        import json
        await self.send(text_data=json.dumps({
            "type": "chat_message_status",
            "message_id": event["message_id"],
            "is_sent": event["is_sent"],
        }))


class NotificationsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if isinstance(user, AnonymousUser) or not user.is_authenticated:
            await self.close()
            return

        # защита от двойного connect для одного channel_name
        if getattr(self, "already_connected", False):
            await self.close()
            return
        self.already_connected = True

        self.groups_list = []

        # Личная группа пользователя — для push_toast
        personal_group = f"user_notifications_{user.id}"
        await self.channel_layer.group_add(personal_group, self.channel_name)
        self.groups_list.append(personal_group)

        # Глобальный канал — все сотрудники получают уведомления о новых клиентах
        await self.channel_layer.group_add("all_employees_notifications", self.channel_name)
        self.groups_list.append("all_employees_notifications")

        # Группы клиентов сотрудника — для push_client_toast
        try:
            employee = await sync_to_async(Employee.objects.get)(user=user)
            client_ids = await sync_to_async(
                lambda: list(
                    Client.objects.filter(employees=employee).values_list("id", flat=True)
                )
            )()
            for cid in client_ids:
                group_name = f"client_ops_{cid}"
                await self.channel_layer.group_add(group_name, self.channel_name)
                self.groups_list.append(group_name)
        except Employee.DoesNotExist:
            pass

        await self.accept()

    async def disconnect(self, code):
        for group in getattr(self, "groups_list", []):
            await self.channel_layer.group_discard(group, self.channel_name)

    async def notify(self, event):
        await self.send(text_data=event["html"])
