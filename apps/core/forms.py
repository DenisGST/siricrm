from django import forms
from django.contrib.auth.models import User
from .models import Department, Employee, MenuItem, Widget, DashboardConfig
from apps.crm.models import Region, LegalEntityKind


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


class EmployeeCreateForm(forms.Form):
    last_name = forms.CharField(max_length=150, label="Фамилия")
    first_name = forms.CharField(max_length=150, label="Имя")
    patronymic = forms.CharField(max_length=255, required=False, label="Отчество")
    username = forms.CharField(max_length=150, label="Логин")
    password = forms.CharField(
        widget=forms.PasswordInput, min_length=6, label="Пароль",
    )
    email = forms.EmailField(required=False, label="Email")
    phone_mobile = forms.CharField(max_length=20, required=False, label="Мобильный телефон")
    phone_internal = forms.CharField(max_length=10, required=False, label="Внутренний номер")
    department = forms.ModelChoiceField(
        queryset=Department.objects.filter(is_active=True),
        required=False, label="Отдел",
    )
    role = forms.ChoiceField(choices=Employee.ROLE_CHOICES, label="Роль")
    dashboard_config = forms.ModelChoiceField(
        queryset=DashboardConfig.objects.filter(is_active=True),
        required=False, label="Конфигурация дашборда",
    )
    has_messenger_access = forms.BooleanField(required=False, initial=True, label="Доступ к мессенджеру")

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Пользователь с таким логином уже существует.")
        return username


class MenuItemForm(forms.ModelForm):
    class Meta:
        model = MenuItem
        fields = [
            "name", "icon", "url", "section", "order", "use_htmx",
            "requires_superuser", "requires_elevated", "is_active",
        ]


class WidgetForm(forms.ModelForm):
    class Meta:
        model = Widget
        fields = ["name", "slug", "widget_type", "template_name", "description", "order", "is_active"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }


class RegionForm(forms.ModelForm):
    class Meta:
        model = Region
        fields = ["number", "name", "court_name", "court_address", "court_payment_details"]
        widgets = {
            "court_address": forms.Textarea(attrs={"rows": 2}),
            "court_payment_details": forms.Textarea(attrs={"rows": 4}),
        }


class LegalEntityKindForm(forms.ModelForm):
    class Meta:
        model = LegalEntityKind
        fields = ["name", "short_name"]


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
