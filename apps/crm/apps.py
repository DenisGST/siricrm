from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.crm'

    def ready(self):
        try:
            from django.template.loader import get_template
            for t in [
                "crm/services/form_modal.html",
                "crm/kanban.html",
                "crm/kanban_my.html",
                "crm/kanban_services.html",
                "crm/partials/kanban_card.html",
                "crm/partials/kanban_services_column.html",
            ]:
                get_template(t)
        except Exception:
            pass
