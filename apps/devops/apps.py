from django.apps import AppConfig


class DevopsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.devops"
    verbose_name = "DevOps Panel"

    def ready(self):
        # Регистрация handlers (импорт триггерит @register_handler)
        from . import handlers  # noqa: F401
