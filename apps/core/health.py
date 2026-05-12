"""Healthcheck endpoint: проверяет БД и Redis."""
from django.db import connection
from django.core.cache import cache
from django.http import JsonResponse


def health_check(request):
    checks = {"db": False, "redis": False}

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["db"] = True
    except Exception:
        pass

    try:
        cache.set("__healthcheck__", "ok", 5)
        checks["redis"] = cache.get("__healthcheck__") == "ok"
    except Exception:
        pass

    ok = all(checks.values())
    return JsonResponse({"status": "ok" if ok else "degraded", **checks},
                        status=200 if ok else 503)
