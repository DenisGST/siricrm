# apps/crm/forms.py
from django import forms
from .models import Client


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = [
            "first_name",
            "last_name",
            "username",
            "telegram_id",
            "phone",
            "email",
            "assigned_operator",
            "status",
            "notes",
        ]