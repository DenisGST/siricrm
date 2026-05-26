"""Middleware: автологаут при бездействии IDLE_TIMEOUT минут.

При каждом HTTP-запросе авторизованного пользователя проверяет
session['last_activity']. Если разница со now > IDLE_TIMEOUT, выполняет
django.contrib.auth.logout(request), помечает причину в session перед
вызовом (используется в core.signals.on_logout для записи EmployeeLog).
"""
from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.http import HttpResponse
from django.utils import timezone

IDLE_TIMEOUT_MINUTES = getattr(settings, "IDLE_TIMEOUT_MINUTES", 5)

# Фоновые / служебные пути, которые НЕ считаются «активностью» юзера:
# их периодически опрашивают HTMX-таймеры, кнопки-индикаторы и т.п.,
# и они не должны сбрасывать idle-таймер.
IDLE_IGNORE_PREFIXES = (
    "/api/notifications/",
    "/api/stats/",
    "/static/",
    "/media/",
    "/health/",
    "/ws/",
)


class IdleAutoLogoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            # Фоновые поллеры не считаем активностью — пропускаем без
            # обновления last_activity и без принудительного logout
            # (иначе при пробуждении после idle браузер получит redirect
            # на /login/ от фонового запроса, что выглядит странно).
            if any(request.path.startswith(p) for p in IDLE_IGNORE_PREFIXES):
                return self.get_response(request)

            now_ts = timezone.now().timestamp()
            last = request.session.get("last_activity")
            if last is not None:
                idle_sec = now_ts - float(last)
                if idle_sec > IDLE_TIMEOUT_MINUTES * 60:
                    request.session["logout_reason"] = (
                        f"автовыход после {IDLE_TIMEOUT_MINUTES} мин бездействия"
                    )
                    auth_logout(request)
                    return self.get_response(request)
            request.session["last_activity"] = now_ts
        return self.get_response(request)


class HtmxLoginRedirectMiddleware:
    """HTMX по умолчанию не делает full reload при 302 redirect → /login/.
    Если view вернул такой редирект, перехватываем и отвечаем 204 +
    `HX-Redirect: /accounts/login/` — браузер перезагрузит страницу
    и юзер увидит нормальную страницу входа, а не вложенный login-форм
    поверх чата/канбана.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        resp = self.get_response(request)
        if not request.headers.get("HX-Request"):
            return resp
        if resp.status_code not in (301, 302):
            return resp
        loc = ""
        try:
            loc = resp.headers.get("Location", "")
        except Exception:
            loc = resp.get("Location", "") if hasattr(resp, "get") else ""
        if "/accounts/login/" not in loc:
            return resp
        new = HttpResponse(status=204)
        new["HX-Redirect"] = loc
        return new
