from django import forms
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Department, Employee, MenuItem, Widget, DashboardConfig
from apps.crm.models import (
    Region, LegalEntityKind,
    ServiceName, PaymentProcedure, ServiceCommonStatus,
    ServiceEmployeeStatus, ServiceTag, MessageTemplate,
    EventType, ActionType,
)


class EmployeeForm(forms.ModelForm):
    user = forms.ModelChoiceField(
        queryset=User.objects.all(),
        label="Пользователь",
    )

    class Meta:
        model = Employee
        fields = ["user", "department", "role", "is_active"]


class _FullNameUserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        full = obj.get_full_name().strip()
        return full if full else obj.username


class DepartmentForm(forms.ModelForm):
    manager = _FullNameUserChoiceField(
        queryset=User.objects.filter(is_active=True).order_by("last_name", "first_name"),
        required=False,
        label="Руководитель",
    )

    class Meta:
        model = Department
        fields = ["name", "description", "manager", "is_active", "sees_all_clients"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }


class EmployeeAdminForm(forms.ModelForm):
    services_allowed = forms.ModelMultipleChoiceField(
        queryset=ServiceName.objects.filter(is_active=True).order_by("short_name"),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Доступные услуги",
    )

    class Meta:
        model = Employee
        fields = [
            "department", "role", "dashboard_config",
            "has_messenger_access", "services_allowed", "is_active",
        ]


class EmployeeFullEditForm(forms.ModelForm):
    """Полная форма редактирования сотрудника (ФИО, контакты, роль, доступы)."""
    last_name = forms.CharField(max_length=150, label="Фамилия")
    first_name = forms.CharField(max_length=150, label="Имя")
    email = forms.EmailField(required=False, label="Email")
    # Статус сотрудника. «Уволен» — инверсия Employee.is_active; отдельным
    # модельным полем не делаем, храним через is_active + dismiss_at.
    is_dismissed = forms.BooleanField(required=False, label="Уволен")
    services_allowed = forms.ModelMultipleChoiceField(
        queryset=ServiceName.objects.filter(is_active=True).order_by("short_name"),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Доступные услуги",
    )

    class Meta:
        model = Employee
        fields = [
            "department", "role", "dashboard_config",
            "has_messenger_access", "accept_telegram_leads", "is_owner",
            "patronymic",
            "phone_mobile", "phone_internal",
            "services_allowed",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["last_name"].initial = self.instance.user.last_name
            self.fields["first_name"].initial = self.instance.user.first_name
            self.fields["email"].initial = self.instance.user.email
            self.fields["is_dismissed"].initial = not self.instance.is_active

    def save(self, commit=True):
        emp = super().save(commit=False)
        emp.user.last_name = self.cleaned_data["last_name"]
        emp.user.first_name = self.cleaned_data["first_name"]
        emp.user.email = self.cleaned_data.get("email", "")
        emp.user.save(update_fields=["last_name", "first_name", "email"])

        dismissed = self.cleaned_data.get("is_dismissed", False)
        emp.is_active = not dismissed
        if dismissed and emp.dismiss_at is None:
            emp.dismiss_at = timezone.now()
        elif not dismissed:
            emp.dismiss_at = None
        # Учётка django — уволенный не должен входить в систему.
        if emp.user.is_active != (not dismissed):
            emp.user.is_active = not dismissed
            emp.user.save(update_fields=["is_active"])

        if commit:
            emp.save()
            self.save_m2m()
        return emp


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
    """Форма редактирования региона.
    Поле court_address — FK на Address — редактируется отдельным блоком
    (структурированные поля адреса) в шаблоне, а не через textarea.
    """
    court_address_text = forms.CharField(
        label="Адрес суда",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Полный адрес (сохранится в Address.source/result)",
    )

    class Meta:
        model = Region
        fields = [
            "number", "name", "court_name",
            "court_payment_details", "court_deposit_details",
        ]
        widgets = {
            "court_payment_details": forms.Textarea(attrs={"rows": 4}),
            "court_deposit_details": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.court_address:
            self.fields["court_address_text"].initial = (
                self.instance.court_address.source
                or self.instance.court_address.result
                or ""
            )

    def save(self, commit=True):
        from apps.crm.models import Address
        region = super().save(commit=False)
        text = (self.cleaned_data.get("court_address_text") or "").strip()

        if text:
            addr = region.court_address
            if addr is None:
                addr = Address.objects.create(
                    client=None, address_type="default",
                    source=text, result=text,
                )
            else:
                addr.source = text
                addr.result = text
                addr.save(update_fields=["source", "result"])
            region.court_address = addr
        elif region.court_address is not None:
            # Очистка текста — оставляем адрес-объект, но обнуляем FK у региона.
            region.court_address = None

        if commit:
            region.save()
        return region


class LegalEntityKindForm(forms.ModelForm):
    class Meta:
        model = LegalEntityKind
        fields = ["name", "short_name"]


class ServiceNameForm(forms.ModelForm):
    departments = forms.ModelMultipleChoiceField(
        queryset=Department.objects.filter(is_active=True),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Отделы",
    )

    class Meta:
        model = ServiceName
        fields = ["full_name", "short_name", "is_active", "departments"]


class PaymentProcedureForm(forms.ModelForm):
    class Meta:
        model = PaymentProcedure
        fields = ["full_name", "short_name", "description", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class ServiceCommonStatusForm(forms.ModelForm):
    class Meta:
        model = ServiceCommonStatus
        fields = ["service_name", "department", "name", "order", "is_active"]


class ServiceEmployeeStatusForm(forms.ModelForm):
    class Meta:
        model = ServiceEmployeeStatus
        fields = ["employee", "common_status", "name", "comment", "order", "is_active"]
        widgets = {"comment": forms.Textarea(attrs={"rows": 2})}


class ServiceTagForm(forms.ModelForm):
    class Meta:
        model = ServiceTag
        fields = ["employee", "name", "color", "is_active"]


class MessageTemplateForm(forms.ModelForm):
    """Шаблон сообщения. Каналы — через MultipleChoice, сохраняем как list в JSON.

    WA-only поля видны/обязательны только если выбран канал 'whatsapp'.
    """

    channels = forms.MultipleChoiceField(
        label="Каналы",
        choices=MessageTemplate.CHANNEL_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        help_text="Где можно использовать этот шаблон.",
    )

    class Meta:
        model = MessageTemplate
        fields = [
            "name", "body", "channels", "is_active",
            "whatsapp_category", "whatsapp_language",
            "whatsapp_meta_id", "whatsapp_meta_status", "whatsapp_meta_rejection",
        ]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 6}),
            "whatsapp_meta_rejection": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        cleaned = super().clean()
        channels = cleaned.get("channels") or []
        cleaned["channels"] = list(channels)
        if "whatsapp" in channels:
            if not cleaned.get("whatsapp_category"):
                self.add_error("whatsapp_category", "Обязательно для WhatsApp-шаблона.")
            if not cleaned.get("whatsapp_language"):
                cleaned["whatsapp_language"] = "ru"
        return cleaned


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


class EventTypeForm(forms.ModelForm):
    """Форма EventType. standard_actions — M2M-подсказки «какие действия
    обычно совершают при этом событии» (используется в модалке лога)."""
    standard_actions = forms.ModelMultipleChoiceField(
        queryset=ActionType.objects.filter(is_active=True).order_by("order", "name"),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Стандартные действия",
    )

    class Meta:
        model = EventType
        fields = ["code", "name", "source", "order",
                  "description", "is_active", "standard_actions"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }


class ActionTypeForm(forms.ModelForm):
    """Форма ActionType. spawns_event — опционально порождаемое событие."""
    class Meta:
        model = ActionType
        fields = ["code", "name", "order", "description",
                  "spawns_event", "is_active"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }
