from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.core'

    def ready(self):
        # Подключить сигналы login/logout → EmployeeLog + is_online.
        from . import signals  # noqa: F401
