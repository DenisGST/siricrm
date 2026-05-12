"""Production-настройки. DEBUG=False + жёсткая безопасность."""
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.celery import CeleryIntegration
from decouple import config

from .base import *  # noqa: F401,F403

DEBUG = False

# В проде SECRET_KEY обязателен (без fallback)
SECRET_KEY = config("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set in production")

ALLOWED_HOSTS = config("ALLOWED_HOSTS").split(",")
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS").split(",")

# --- HTTPS / Cookies ---
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000  # 1 год
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"

# --- Sentry ---
SENTRY_DSN = config("SENTRY_DSN", default="")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        traces_sample_rate=0.1,
        send_default_pii=False,
        environment=config("SENTRY_ENVIRONMENT", default="production"),
    )

# --- CORS: в проде только явно разрешённые ---
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="").split(",")
CORS_ALLOWED_ORIGINS = [o for o in CORS_ALLOWED_ORIGINS if o]

# --- В проде логируем WARNING+ для django ---
LOGGING["loggers"]["django"]["level"] = "WARNING"  # noqa: F405
