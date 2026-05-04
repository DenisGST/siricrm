from django import forms
from .models import ConsultationResult

class ConsultationResultForm(forms.ModelForm):
    class Meta:
        model  = ConsultationResult
        fields = ["name", "color", "order", "is_active"]
