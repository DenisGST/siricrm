from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from apps.core.models import TimeStampedModel
import uuid


class TelegramUser(TimeStampedModel):
    """Telegram user session for authentication"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    telegram_id = models.BigIntegerField(unique=True, verbose_name='Telegram ID')
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='telegram_user',
        verbose_name='Django User'
    )
    first_name = models.CharField(max_length=255, verbose_name='Имя')
    last_name = models.CharField(max_length=255, blank=True, verbose_name='Фамилия')
    username = models.CharField(max_length=255, blank=True, verbose_name='Username')
    
    auth_token = models.CharField(max_length=255, blank=True, verbose_name='Auth Token')
    is_verified = models.BooleanField(default=False, verbose_name='Подтвержден')
    
    last_login = models.DateTimeField(default=timezone.now, verbose_name='Последний вход')

    def __str__(self):
        return f"{self.first_name} (@{self.username})"

    class Meta:
        verbose_name = 'Пользователь Telegram'
        verbose_name_plural = 'Пользователи Telegram'


class TelegramAuthCode(models.Model):
    code = models.CharField(max_length=64, unique=True)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="telegram_auth_code",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    def is_expired(self, minutes: int = 10) -> bool:
        return self.created_at < timezone.now() - timezone.timedelta(minutes=minutes)

    def __str__(self):
        return f"{self.user} - {self.code}"