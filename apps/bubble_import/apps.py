from django.apps import AppConfig


class BubbleImportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.bubble_import"
    verbose_name = "Импорт из Bubble.io"
