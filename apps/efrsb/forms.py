"""Формы справочника ЕФРСБ."""
from __future__ import annotations

from django import forms

from .models import KIND_REALIZATION, KIND_RESTRUCTURING, EfrsbMessageType


class EfrsbMessageTypeForm(forms.ModelForm):
    """Тип сообщения ЕФРСБ. JSON-поля (алиасы/виды) — через удобный ввод."""

    api_type_aliases_csv = forms.CharField(
        label="Доп. типы API (через запятую)", required=False,
        widget=forms.TextInput(attrs={"class": "input input-bordered input-sm w-full",
                                      "placeholder": "Meeting2, ChangeAuction"}),
    )
    applicable_kinds_set = forms.MultipleChoiceField(
        label="Применим к видам процедур", required=False,
        choices=[(KIND_RESTRUCTURING, "Реструктуризация"), (KIND_REALIZATION, "Реализация")],
        widget=forms.CheckboxSelectMultiple,
        help_text="Пусто — применим к обоим.",
    )

    class Meta:
        model = EfrsbMessageType
        fields = [
            "code", "name", "description", "api_kind", "api_type",
            "template", "isk_template",
            "deadline_base_key", "deadline_offset_days",
            "order", "is_active", "is_draft",
        ]
        widgets = {
            "code": forms.TextInput(attrs={"class": "input input-bordered input-sm w-full"}),
            "name": forms.TextInput(attrs={"class": "input input-bordered input-sm w-full"}),
            "description": forms.Textarea(attrs={"class": "textarea textarea-bordered textarea-sm w-full", "rows": 2}),
            "api_kind": forms.Select(attrs={"class": "select select-bordered select-sm w-full"}),
            "api_type": forms.TextInput(attrs={"class": "input input-bordered input-sm w-full"}),
            "template": forms.Select(attrs={"class": "select select-bordered select-sm w-full"}),
            "isk_template": forms.Select(attrs={"class": "select select-bordered select-sm w-full"}),
            "deadline_base_key": forms.TextInput(attrs={"class": "input input-bordered input-sm w-full"}),
            "deadline_offset_days": forms.NumberInput(attrs={"class": "input input-bordered input-sm w-full"}),
            "order": forms.NumberInput(attrs={"class": "input input-bordered input-sm w-24"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["api_type_aliases_csv"].initial = ", ".join(self.instance.api_type_aliases or [])
            self.fields["applicable_kinds_set"].initial = self.instance.applicable_kinds or []
        # Пустой выбор шаблона.
        self.fields["template"].required = False
        self.fields["isk_template"].required = False

    def save(self, commit=True):
        obj = super().save(commit=False)
        raw = (self.cleaned_data.get("api_type_aliases_csv") or "").strip()
        obj.api_type_aliases = [s.strip() for s in raw.split(",") if s.strip()]
        obj.applicable_kinds = list(self.cleaned_data.get("applicable_kinds_set") or [])
        if commit:
            obj.save()
        return obj
