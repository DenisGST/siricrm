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
            "patronymic",
            "username",
            "telegram_id",
            "phone",
            "email",
            "birth_date",
            "birth_place",
            "passport_series",
            "passport_number",
            "passport_issued_by",
            "passport_issued_date",
            "inn",
            "snils",
            "employees",
            "status",
            "notes",
        ]
        widgets = {
            "birth_date": forms.DateInput(attrs={"type": "date", "class": "input input-bordered w-full"}),
            "passport_issued_date": forms.DateInput(attrs={"type": "date", "class": "input input-bordered w-full"}),
        }
