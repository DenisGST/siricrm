from django import forms
from django.contrib.auth.models import User
from .models import Department, Employee, MenuItem, Widget, DashboardConfig


class EmployeeForm(forms.ModelForm):
    user = forms.ModelChoiceField(
        queryset=User.objects.all(),
        label="Пользователь",
    )

    class Meta:
        model = Employee
        fields = ["user", "department", "role", "is_active"]


class DepartmentForm(forms.ModelForm):
    manager = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True),
        required=False,
        label="Руководитель",
    )

    class Meta:
        model = Department
        fields = ["name", "description", "manager", "is_active"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }


class EmployeeAdminForm(forms.ModelForm):
    class Meta:
        model = Employee
        fields = [
            "department", "role", "dashboard_config",
            "has_messenger_access", "is_active",
        ]


class MenuItemForm(forms.ModelForm):
    class Meta:
        model = MenuItem
        fields = ["name", "icon", "url", "section", "order", "use_htmx", "requires_superuser", "is_active"]


class WidgetForm(forms.ModelForm):
    class Meta:
        model = Widget
        fields = ["name", "slug", "widget_type", "template_name", "description", "order", "is_active"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }


class DashboardConfigForm(forms.ModelForm):
    menu_items = forms.ModelMultipleChoiceField(
        queryset=MenuItem.objects.filter(is_active=True),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Пункты меню",
    )
    widgets = forms.ModelMultipleChoiceField(
        queryset=Widget.objects.filter(is_active=True),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Виджеты",
    )

    class Meta:
        model = DashboardConfig
        fields = ["name", "description", "menu_items", "widgets", "is_default", "is_active"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }
