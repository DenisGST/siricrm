"""Формы раздела процедур. Пока — редактирование каталога мероприятий в
«Справочниках» (вместо админки)."""
from django import forms

from .models import MilestoneTemplate


class MilestoneTemplateForm(forms.ModelForm):
    class Meta:
        model = MilestoneTemplate
        fields = [
            "stage", "code", "title", "description",
            "base_date_key", "offset_days", "order",
            "is_mandatory", "is_draft", "is_active", "responsible_role",
        ]
