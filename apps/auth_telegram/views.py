import uuid
import secrets
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.conf import settings
from django.utils import timezone
from .models import TelegramAuthCode
from .models import TelegramUser  # и, возможно, модель VerificationCode

@login_required
def telegram_login(request):
    code_obj, _ = TelegramAuthCode.objects.get_or_create(user=request.user)

    if not code_obj.code or code_obj.is_used or code_obj.is_expired():
        code_obj.code = uuid.uuid4().hex[:8].upper()
        code_obj.is_used = False
        code_obj.created_at = timezone.now()  # ВАЖНО: обновляем время
        code_obj.save()

    bot_username = getattr(settings, "TELEGRAM_BOT_USERNAME", "your_bot_name")

    return render(
        request,
        "auth/telegram_login.html",
        {
            "auth_code": code_obj.code,
            "bot_username": bot_username,
        },
    )