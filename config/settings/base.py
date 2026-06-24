"""Базовые настройки SiriCRM. Общие для dev и prod.

Любые env-зависимые/секретные значения переопределяются в dev.py / prod.py.
"""
from pathlib import Path
import os
from decouple import config
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --- Core ---
SECRET_KEY = config("SECRET_KEY", default="")
DEBUG = False  # переопределяется в dev.py
ALLOWED_HOSTS: list[str] = []
CSRF_TRUSTED_ORIGINS: list[str] = []

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- Apps ---
INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",  # naturaltime/intcomma/etc — используется в arbitr/* шаблонах

    "rest_framework",
    "corsheaders",
    "django_celery_beat",
    "django_celery_results",
    "storages",
    "drf_spectacular",
    "channels",

    "apps.core",
    "apps.crm",
    "apps.files",
    "apps.realtime",
    "apps.telegram",
    "apps.maxchat",
    "apps.consultations",
    "apps.questionnaire",
    "apps.devops",
    "apps.finance",
    "apps.whatsapp",
    "apps.bubble_import",
    "apps.arbitr",
    "apps.afd",
    "apps.scans",
    "apps.accounting",
    "apps.notifications",
    "apps.procedure",

    # django-rules: object-level permissions (apps/<app>/rules.py авто-импортируются)
    "rules.apps.AutodiscoverRulesConfig",
]

# Django по умолчанию использует только ModelBackend (он отвечает за логин).
# Добавляем ObjectPermissionBackend из django-rules — он подключает
# user.has_perm('crm.edit_client', client) и шаблонный тэг {% has_perm %}.
AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",
    "rules.permissions.ObjectPermissionBackend",
)

# --- DevOps panel ---
DEVOPS_AGENT_TOKEN = config("DEVOPS_AGENT_TOKEN", default="")
DEVOPS_AGENT_TOKEN_PROD = config("DEVOPS_AGENT_TOKEN_PROD", default="")

# --- Бухгалтерия / ТБанк (apps.accounting) ---
# Секреты только из env. Пустой токен → поллинг no-op (источник «не настроен»).
TBANK_BUSINESS_API_BASE = config("TBANK_BUSINESS_API_BASE", default="https://business.tbank.ru/openapi")
TBANK_ACQUIRING_API_BASE = config("TBANK_ACQUIRING_API_BASE", default="https://securepay.tinkoff.ru/v2")
TBANK_BUSINESS_API_TOKEN = config("TBANK_BUSINESS_API_TOKEN", default="")
TBANK_ACCOUNT_NUMBER = config("TBANK_ACCOUNT_NUMBER", default="")
TBANK_ACQUIRING_TERMINAL_KEY = config("TBANK_ACQUIRING_TERMINAL_KEY", default="")
TBANK_ACQUIRING_PASSWORD = config("TBANK_ACQUIRING_PASSWORD", default="")
# Гейты поллинга (по умолчанию выключены — включать когда заведены креды).
ACCOUNTING_STATEMENT_POLL_ENABLED = config("ACCOUNTING_STATEMENT_POLL_ENABLED", default=False, cast=bool)
ACCOUNTING_ACQUIRING_POLL_ENABLED = config("ACCOUNTING_ACQUIRING_POLL_ENABLED", default=False, cast=bool)
# Минимальный интервал опроса выписки р/с (часы) — «не чаще раз в N часов».
ACCOUNTING_POLL_MIN_INTERVAL_HOURS = config("ACCOUNTING_POLL_MIN_INTERVAL_HOURS", default=3, cast=int)

MIDDLEWARE = [
    # Должна быть ПЕРВОЙ — её process_response вызывается последним и стирает
    # security-headers для /wa/file/ (без этого WhatsApp Cloud отвергает media).
    "apps.whatsapp.middleware.WAFileProxyHeaderStripMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.IdleAutoLogoutMiddleware",
    "apps.core.middleware.HtmxLoginRedirectMiddleware",
]

# Авто-логаут после N минут бездействия (HTTP-неактивности).
IDLE_TIMEOUT_MINUTES = 10

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.sidebar_menu",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Redis / Cache / Channels ---
REDIS_URL = config("REDIS_URL", default="redis://redis:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# Сессии — в Redis, а не в БД. Это нужно потому что DevOps-команды pull_db/restore_db
# дропают схему public, в которой жил бы django_session — пользователя выкидывало на
# логин в середине операции. Redis при этом не пересоздаётся.
SESSION_ENGINE = "django.contrib.sessions.backends.cache"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    }
}

# --- Database ---
DATABASES = {
    "default": dj_database_url.parse(
        config("DATABASE_URL", default="postgresql://crm_user:crm_password_dev@db:5432/crm_db")
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- I18N / TZ ---
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

# --- Static / Media ---
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
# Django 5.x: settings.STATICFILES_STORAGE deprecated, нужен STORAGES dict.
# Иначе fallback на простой StaticFilesStorage без manifest — без hash в имени
# браузер кэширует CSS «навечно» (immutable от whitenoise), и обновления
# стилей не подхватываются даже на Ctrl+Shift+R.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# --- S3 (Beget Cloud) ---
AWS_ACCESS_KEY_ID = config("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = config("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME")
AWS_S3_REGION_NAME = config("AWS_S3_REGION_NAME", default="us-east-1")
AWS_S3_BASE_URL = config("AWS_S3_BASE_URL")

# Бакет для бэкапов (опционально, если не задан — используется AWS_STORAGE_BUCKET_NAME)
AWS_BACKUP_BUCKET_NAME = config("AWS_BACKUP_BUCKET_NAME", default="")
# Отдельные ключи для backup-бакета (опционально; fallback на AWS_* в коде handler-а)
AWS_BACKUP_ACCESS_KEY_ID = config("AWS_BACKUP_ACCESS_KEY_ID", default="")
AWS_BACKUP_SECRET_ACCESS_KEY = config("AWS_BACKUP_SECRET_ACCESS_KEY", default="")
AWS_BACKUP_S3_BASE_URL = config("AWS_BACKUP_S3_BASE_URL", default="")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- DRF ---
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
}

CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default="http://localhost:3000,http://localhost:8000",
).split(",")
CORS_ALLOW_CREDENTIALS = True

# --- Celery ---
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60

# Routing: задачи <prefix>.* идут в отдельные очереди — каждую обслуживает свой контейнер:
#   devops-runner  — очередь `devops`  (docker.sock, git, compose; обычный python:slim)
#   arbitr-runner  — очередь `arbitr`  (Playwright + Chromium, образ mcr.microsoft.com/playwright)
# Web/celery (общий worker) НЕ слушает эти очереди — задачи туда не попадут случайно.
CELERY_TASK_ROUTES = {
    "devops.*": {"queue": "devops"},
    "arbitr.*": {"queue": "arbitr"},
}

# --- Telegram ---
TELEGRAM_API_ID = config("TELEGRAM_API_ID", default="")
TELEGRAM_API_HASH = config("TELEGRAM_API_HASH", default="")
TELEGRAM_PHONE = config("TELEGRAM_PHONE", default="")
# Токен leads-бота (@Sirius_system_bot). Раньше читался только в leads_bot.py
# через decouple; вынесли в settings — нужен health-монитору для TG-алёртов.
TELEGRAM_BOT_TOKEN = config("TELEGRAM_BOT_TOKEN", default="")

# --- DaData ---
DADATA_API_KEY = config("DADATA_API_KEY", default="")
DADATA_SECRET_KEY = config("DADATA_SECRET_KEY", default="")

# --- MAX bot ---
MAX_BOT_TOKEN = config("MAX_BOT_TOKEN", default="")
MAX_API_BASE_URL = "https://platform-api.max.ru"
MAX_WEBHOOK_SECRET = config("MAX_WEBHOOK_SECRET", default="")

# Внешний публичный URL CRM — нужен для построения absolute-ссылок
# из тасок (где нет request.get_host). Используется в WhatsApp-прокси
# (apps/whatsapp/views.wa_file_proxy) — 1msg.io скачивает медиа по
# этому URL вместо Beget S3 pre-signed (Beget даёт 403 на HEAD).
PUBLIC_BASE_URL = config("PUBLIC_BASE_URL", default="https://crmsiri.ru")

# --- Arbitr (kad.arbitr.ru) parser ---
# Куда слать алёрты при капче / других интерактивных ошибках парсера.
# Пока — один MAX chat_id админа; позже разнесём по Employee.max_chat_id.
ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID = config("ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID", default="")

# --- Мониторинг доступности (apps.core.tasks.monitor_health) ---
# Кросс-серверно: dev мониторит прод, прод мониторит dev. Пусто → выключено.
HEALTH_MONITOR_TARGET_URL = config("HEALTH_MONITOR_TARGET_URL", default="")
HEALTH_MONITOR_LABEL = config("HEALTH_MONITOR_LABEL", default="")
# Опциональный Host-заголовок (если бьём по IP/внутреннему адресу).
HEALTH_MONITOR_HOST = config("HEALTH_MONITOR_HOST", default="")
HEALTH_MONITOR_FAIL_THRESHOLD = config("HEALTH_MONITOR_FAIL_THRESHOLD", default=2, cast=int)
# Куда слать алёрты. MAX по умолчанию переиспользует chat арбитра.
HEALTH_ALERT_MAX_CHAT_ID = (
    config("HEALTH_ALERT_MAX_CHAT_ID", default="") or ARBITR_CAPTCHA_NOTIFY_MAX_CHAT_ID
)
HEALTH_ALERT_TELEGRAM_CHAT_ID = config("HEALTH_ALERT_TELEGRAM_CHAT_ID", default="")

# --- Telegram-бот мониторинга (кнопки «Статус прод»/«Статистика») ---
# Поллер getUpdates крутится там, где MONITOR_BOT_POLL=true (на dev).
MONITOR_BOT_POLL = config("MONITOR_BOT_POLL", default=False, cast=bool)
# Адрес DevOps-агента прода + его токен (dev дёргает status/daily_stats).
PROD_AGENT_URL = config("PROD_AGENT_URL", default="https://siricrm.ru")
DEVOPS_AGENT_TOKEN_PROD = config("DEVOPS_AGENT_TOKEN_PROD", default="")
# Кому разрешён бот (через запятую). Пусто → берётся HEALTH_ALERT_TELEGRAM_CHAT_ID
# (только Каныгин). Fail-closed: если и там пусто — бот не отвечает никому.
MONITOR_BOT_ALLOWED_CHAT_IDS = config("MONITOR_BOT_ALLOWED_CHAT_IDS", default="")
# Headless по умолчанию. Для локальной отладки парсера выставить ARBITR_HEADLESS=false.
ARBITR_HEADLESS = config("ARBITR_HEADLESS", default="true").lower() != "false"

# --- Auth redirects ---
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = LOGIN_URL

# --- Logging ---
os.makedirs(BASE_DIR / "logs", exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "simple": {
            "format": "{levelname} {asctime} {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {"level": "DEBUG", "class": "logging.StreamHandler", "formatter": "simple"},
        "file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "crm.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
        },
        "celery_file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "celery.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
        },
        "userbot_file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "userbot.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
        },
        "maxbot_file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "maxbot.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "django": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["console", "celery_file"], "level": "INFO", "propagate": False},
        "userbot": {"handlers": ["console", "userbot_file"], "level": "INFO", "propagate": False},
        "maxbot": {"handlers": ["maxbot_file"], "level": "INFO", "propagate": False},
    },
}
