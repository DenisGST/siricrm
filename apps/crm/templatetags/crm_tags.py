from django import template

register = template.Library()


@register.filter
def short_name(emp):
    """Форматирует Employee как 'Фамилия И.О.'"""
    if not emp:
        return ""
    user = getattr(emp, "user", emp)  # принимает и Employee, и User
    last  = (user.last_name or "").strip()
    first = (user.first_name or "").strip()
    patr  = (getattr(emp, "patronymic", "") or "").strip()
    if not last and not first:
        return user.username or ""
    result = last
    if first:
        result += f" {first[0]}."
    if patr:
        result += f"{patr[0]}."
    return result
