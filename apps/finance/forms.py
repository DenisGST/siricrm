from django import forms

from . import models


class ExpenseTypeForm(forms.ModelForm):
    class Meta:
        model = models.ExpenseType
        fields = ["service_name", "name", "is_active"]


class IncomeTypeForm(forms.ModelForm):
    class Meta:
        model = models.IncomeType
        fields = ["service_name", "name", "is_active", "is_legal_services"]


class IncomingAccountForm(forms.ModelForm):
    class Meta:
        model = models.IncomingAccount
        fields = ["account_type", "name", "is_active"]


class OutgoingAccountForm(forms.ModelForm):
    class Meta:
        model = models.OutgoingAccount
        fields = ["account_type", "name", "is_active"]


class ChargeForm(forms.ModelForm):
    class Meta:
        model = models.Charge
        fields = ["due_date", "title", "amount", "status", "comments"]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "comments": forms.Textarea(attrs={"rows": 2}),
        }


class PaymentForm(forms.ModelForm):
    """Форма платежа.

    В зависимости от direction оставляем заполненной одну группу полей
    (приход/расход) — это валидируется в clean().
    """

    class Meta:
        model = models.Payment
        fields = [
            "payment_date", "direction",
            "expense_type", "income_type",
            "amount_in", "amount_out",
            "payment_form",
            "incoming_account", "outgoing_account",
            "service", "charge", "comments",
        ]
        widgets = {
            "payment_date": forms.DateInput(attrs={"type": "date"}),
            "comments": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, client=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = client
        if client is not None:
            # Ограничиваем услуги/начисления — только этого клиента.
            self.fields["service"].queryset = client.services.all()
            self.fields["charge"].queryset = client.charges.all()
        self.fields["expense_type"].queryset = models.ExpenseType.objects.filter(is_active=True)
        self.fields["income_type"].queryset = models.IncomeType.objects.filter(is_active=True)
        self.fields["incoming_account"].queryset = models.IncomingAccount.objects.filter(is_active=True)
        self.fields["outgoing_account"].queryset = models.OutgoingAccount.objects.filter(is_active=True)

    def clean(self):
        cleaned = super().clean()
        direction = cleaned.get("direction")
        if direction == "in":
            if not cleaned.get("amount_in"):
                self.add_error("amount_in", "Укажите сумму входящего платежа.")
            if not cleaned.get("income_type"):
                self.add_error("income_type", "Выберите тип дохода.")
            if not cleaned.get("incoming_account"):
                self.add_error("incoming_account", "Укажите, куда поступил платёж.")
            cleaned["amount_out"] = None
            cleaned["expense_type"] = None
            cleaned["outgoing_account"] = None
        elif direction == "out":
            if not cleaned.get("amount_out"):
                self.add_error("amount_out", "Укажите сумму исходящего платежа.")
            if not cleaned.get("expense_type"):
                self.add_error("expense_type", "Выберите тип расхода.")
            if not cleaned.get("outgoing_account"):
                self.add_error("outgoing_account", "Укажите, откуда произведена оплата.")
            cleaned["amount_in"] = None
            cleaned["income_type"] = None
            cleaned["incoming_account"] = None
        return cleaned
