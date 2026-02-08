from django import forms
from django.contrib.auth.models import User  # или твой кастомный User
from .models import Employee

class EmployeeForm(forms.ModelForm):
    user = forms.ModelChoiceField(
        queryset=User.objects.all(),
        label="Пользователь",
    )

    class Meta:
        model = Employee
        fields = ["user", "department", "role", "is_active"]
