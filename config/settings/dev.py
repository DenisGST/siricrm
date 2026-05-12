"""Dev-настройки. DEBUG=True, всё разрешено локально."""
from decouple import config
from .base import *  # noqa: F401,F403

DEBUG = True

ALLOWED_HOSTS = config(
    "ALLOWED_HOSTS",
    default="localhost,127.0.0.1,0.0.0.0",
).split(",")

CSRF_TRUSTED_ORIGINS = config(
    "CSRF_TRUSTED_ORIGINS",
    default="http://localhost:8000,http://127.0.0.1:8000",
).split(",")

# Dev: SECRET_KEY имеет fallback, чтобы не падать без .env
import os
if not os.environ.get("SECRET_KEY"):
    SECRET_KEY = "django-insecure-dev-key-change-in-production"  # noqa: S105
