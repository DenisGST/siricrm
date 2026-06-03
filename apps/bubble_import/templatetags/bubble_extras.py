from django import template

register = template.Library()


@register.filter
def bv(record, key):
    """Значение поля Bubble-записи с учётом overrides: {{ rec|bv:'fName' }}."""
    try:
        v = record.value(key, "")
    except Exception:
        return ""
    return "" if v is None else v
