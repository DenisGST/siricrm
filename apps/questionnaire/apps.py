from django.apps import AppConfig


class QuestionnaireConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.questionnaire"
    label = "questionnaire"

    def ready(self):
        # Прогрев шаблонов при старте: Django компилирует шаблон один раз,
        # затем кеширует — первые реальные запросы не тормозят.
        try:
            from django.template.loader import get_template
            for t in [
                "questionnaire/quiz/step.html",
                "questionnaire/quiz/question.html",
                "questionnaire/quiz/partials/marital_status.html",
                "questionnaire/quiz/partials/spouse_fields.html",
                "questionnaire/quiz/partials/bank_debts.html",
                "questionnaire/quiz/partials/bank_entry.html",
                "questionnaire/quiz/partials/mfo_debts.html",
                "questionnaire/quiz/partials/mfo_entry.html",
                "crm/services/form_modal.html",
            ]:
                get_template(t)
        except Exception:
            pass
