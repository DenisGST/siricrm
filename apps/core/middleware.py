"""Middleware: автологаут при бездействии IDLE_TIMEOUT минут.

При каждом HTTP-запросе авторизованного пользователя проверяет
session['last_activity']. Если разница со now > IDLE_TIMEOUT, выполняет
django.contrib.auth.logout(request), помечает причину в session перед
вызовом (используется в core.signals.on_logout для записи EmployeeLog).
"""
from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.utils import timezone

IDLE_TIMEOUT_MINUTES = getattr(settings, "IDLE_TIMEOUT_MINUTES", 5)


class IdleAutoLogoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            now_ts = timezone.now().timestamp()
            last = request.session.get("last_activity")
            if last is not None:
                idle_sec = now_ts - float(last)
                if idle_sec > IDLE_TIMEOUT_MINUTES * 60:
                    # Передаём причину в signal core.on_logout (там пишется лог).
                    request.session["logout_reason"] = (
                        f"автовыход после {IDLE_TIMEOUT_MINUTES} мин бездействия"
                    )
                    auth_logout(request)
                    # session очищается — last_activity больше не нужен.
                    return self.get_response(request)
            request.session["last_activity"] = now_ts
        return self.get_response(request)
