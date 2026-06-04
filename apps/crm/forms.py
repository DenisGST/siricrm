# apps/crm/forms.py
from django import forms
from .models import (
    Client, LegalEntity,
    Service, ServiceName, PaymentProcedure, ServiceCommonStatus, Region,
)
from apps.core.models import Employee


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = [
            "first_name",
            "last_name",
            "patronymic",
            "username",
            "phone",
            "email",
            "birth_date",
            "birth_place",
            "passport_series",
            "passport_number",
            "passport_issued_by",
            "passport_issued_date",
            "passport_division_code",
            "inn",
            "snils",
            "status",
            "notes",
        ]
        widgets = {
            "birth_date": forms.DateInput(attrs={"type": "date", "class": "input input-bordered w-full"}),
            "passport_issued_date": forms.DateInput(attrs={"type": "date", "class": "input input-bordered w-full"}),
        }

    # Выставляется при конфликте телефона — модалка показывает баннер
    # со ссылкой «открыть существующего».
    phone_conflict = None

    def clean_phone(self):
        """Дедуп: не даём создать/сохранить клиента с телефоном, который уже
        принадлежит ДРУГОМУ клиенту (по ClientPhone либо legacy Client.phone)."""
        raw = (self.cleaned_data.get("phone") or "").strip()
        if not raw:
            return raw
        from .phone_utils import normalize_phone, find_client_by_phone, format_phone
        norm = normalize_phone(raw)
        if not norm:
            return raw  # невалидный формат не блокируем — сохраняем как ввели
        self_pk = getattr(self.instance, "pk", None)
        conflict = find_client_by_phone(norm)
        if conflict is None or conflict.pk == self_pk:
            # fallback на legacy Client.phone без ClientPhone (ручные клиенты,
            # созданные после backfill crm.0065) — сравниваем по 10 цифрам.
            qs = Client.objects.filter(phone__contains=norm[1:])
            if self_pk:
                qs = qs.exclude(pk=self_pk)
            conflict = qs.first()
        if conflict is not None and conflict.pk != self_pk:
            self.phone_conflict = conflict
            fio = f"{conflict.last_name} {conflict.first_name}".strip() or "без ФИО"
            raise forms.ValidationError(
                f"Телефон уже у клиента «{fio}» — дубликат создавать нельзя, "
                f"откройте существующего."
            )
        return format_phone(raw)  # храним в виде +7 (XXX) XXX-XX-XX


class LegalEntityForm(forms.ModelForm):
    class Meta:
        model = LegalEntity
        fields = [
            "kind", "region", "entity_type", "name", "short_name", "brand",
            "inn", "kpp", "ogrn", "okpo", "okved",
            "legal_address", "actual_address", "postal_address",
            "director_name", "director_title",
            "phone", "email", "website",
            "bank_name", "bik", "correspondent_account", "settlement_account",
            "notes", "is_active", "status",
        ]


class ServiceForm(forms.ModelForm):
    _sel = {"class": "select select-bordered select-sm w-full"}
    name = forms.ModelChoiceField(
        queryset=ServiceName.objects.filter(is_active=True).order_by("short_name"),
        label="Услуга",
        widget=forms.Select(attrs={**_sel, "form": "svc-form"}),
    )
    region = forms.ModelChoiceField(
        queryset=Region.objects.order_by("number"),
        required=False, label="Регион",
        widget=forms.Select(attrs=_sel),
    )
    payment_procedure = forms.ModelChoiceField(
        queryset=PaymentProcedure.objects.filter(is_active=True).order_by("short_name"),
        required=False, label="Порядок оплаты",
        widget=forms.Select(attrs=_sel),
    )
    common_status = forms.ModelChoiceField(
        queryset=ServiceCommonStatus.objects.filter(is_active=True),
        required=False, label="Общий статус услуги",
        widget=forms.Select(attrs={**_sel, "form": "svc-form"}),
    )
    agent = forms.ModelChoiceField(
        queryset=Client.objects.none(), required=False, label="Агент",
    )

    class Meta:
        model = Service
        fields = [
            "client", "agent", "name", "region",
            "agent_circs", "agent_once_amount", "agent_percent", "agent_notes",
            "date_dogovor", "numb_dogovor",
            "date_start", "date_end", "date_terminated", "date_executed",
            "contract_price", "payment_procedure", "common_status", "is_active",
        ]
        widgets = {
            "date_dogovor": forms.DateInput(attrs={"type": "date"}),
            "date_start": forms.DateInput(attrs={"type": "date"}),
            "date_end": forms.DateInput(attrs={"type": "date"}),
            "date_terminated": forms.DateInput(attrs={"type": "date"}),
            "date_executed": forms.DateInput(attrs={"type": "date"}),
            "agent_notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, current_employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_employee = current_employee
        self.fields["common_status"].queryset = ServiceCommonStatus.objects.filter(
            is_active=True,
        ).order_by("order", "name")

        # agent выбирается живым поиском — грузим только нужного клиента, не всю базу
        agent_pk = (
            self.data.get("agent")
            or (self.instance.agent_id if self.instance and self.instance.pk else None)
        )
        self.fields["agent"].queryset = (
            Client.objects.filter(pk=agent_pk) if agent_pk else Client.objects.none()
        )

    def clean_name(self):
        sn = self.cleaned_data.get("name")
        if sn and self.current_employee:
            if not self.current_employee.services_allowed.filter(pk=sn.pk).exists():
                raise forms.ValidationError(
                    "У вас нет доступа к этой услуге. Обратитесь к администратору."
                )
        return sn
