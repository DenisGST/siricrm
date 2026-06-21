"""Формы раздела процедур — редактирование каталогов в «Справочниках»."""
from django import forms

from .models import MilestoneTemplate, RequestPackage, RequestType


class MilestoneTemplateForm(forms.ModelForm):
    class Meta:
        model = MilestoneTemplate
        fields = [
            "stage", "code", "title", "description",
            "base_date_key", "offset_days", "order",
            "is_mandatory", "is_draft", "is_active", "responsible_role",
        ]


class RequestTypeForm(forms.ModelForm):
    # default_recipient ставится во вью из typeahead (recipient_id), не как select.
    class Meta:
        model = RequestType
        fields = ["code", "name", "response_days", "order", "is_active", "is_draft"]


class RequestPackageForm(forms.ModelForm):
    class Meta:
        model = RequestPackage
        fields = ["code", "name", "types", "order", "is_active", "is_draft"]
        widgets = {"types": forms.CheckboxSelectMultiple}
