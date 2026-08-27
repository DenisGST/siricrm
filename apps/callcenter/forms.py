from django import forms

from .models import CallCenterColumn, CallResult


class CallCenterColumnForm(forms.ModelForm):
    """Форма колонки доски.

    🛑 `order` и `wip_limit` — PositiveIntegerField с default, а Django делает
    такие поля формы ОБЯЗАТЕЛЬНЫМИ (в отличие от BooleanField, которому сам
    ставит required=False). Без этих переопределений форма молча падала на
    валидации, когда админ оставлял «Порядок» пустым, чтобы колонка встала
    в конец.
    """
    order = forms.IntegerField(label="Порядок", required=False, min_value=0)
    wip_limit = forms.IntegerField(label="Лимит карточек", required=False, min_value=0)

    class Meta:
        model = CallCenterColumn
        fields = ["name", "description", "color", "order", "wip_limit",
                  "is_default", "catch_unknown_calls", "catch_telegram_leads",
                  "is_active"]

    def clean_wip_limit(self):
        return self.cleaned_data.get("wip_limit") or 0


class BlockedPhoneForm(forms.Form):
    """Ручное добавление номера в чёрный список.

    🛑 Намеренно НЕ ModelForm: у ``BlockedPhone.phone`` стоит unique, и
    ModelForm отвечал бы ошибкой на повторный ввод уже заблокированного
    номера. А повторный ввод — нормальное действие («он снова звонит»),
    и ``intake.block_phone`` обрабатывает его идемпотентно.

    Формат номера тут не проверяем: приводит его к общему виду
    ``intake.blacklist_key``, он же и решает, разобрался номер или нет.
    """
    phone = forms.CharField(label="Номер", max_length=32)
    comment = forms.CharField(label="Причина", max_length=255, required=False)


class CallResultForm(forms.ModelForm):
    """Справочник результатов звонка. `order` необязателен — как у колонок."""
    order = forms.IntegerField(label="Порядок", required=False, min_value=0)

    class Meta:
        model = CallResult
        fields = ["name", "hint", "color", "order", "suggest_next_action", "is_active"]
