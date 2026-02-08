# apps/crm/forms.py
from django import forms
from .models import Client
from apps.core.models import Employee


class ClientForm(forms.ModelForm):
    employees = forms.ModelMultipleChoiceField(
        queryset=Employee.objects.filter(is_active=True),
        required=False,
        widget=forms.SelectMultiple(
            attrs={
                "class": "select select-bordered w-full",
                "size": 5,
            }
        ),
        label="Сотрудники",
        help_text="Выберите одного или нескольких сотрудников",
    )

    class Meta:
        model = Client
        fields = [
            "first_name",
            "last_name",
            "username",
            "telegram_id",
            "phone",
            "email",
            "employees",  # вместо assigned_employee
            "status",
            "notes",
        ]
