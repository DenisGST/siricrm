from django import template

register = template.Library()


@register.filter
def question_key(q):
    """Возвращает имя поля формы для вопроса: q_<pk>"""
    return f"q_{q.pk}"


@register.filter
def answer_for(answers, question):
    """Возвращает dict-ответ для вопроса из словаря answers."""
    if not answers:
        return {}
    return answers.get(str(question.pk)) or {}


@register.filter
def answer_val(ans):
    """Возвращает основное значение из dict-ответа."""
    if not ans:
        return ""
    return ans.get("v") or ans.get("ref") or ans.get("fio") or ""


@register.filter
def ms_entry(index):
    """Возвращает 'd{index}' — идентификатор записи о разводе."""
    return f"d{index}"


@register.filter
def mc_extra(ans, choice_pk):
    """Возвращает сохранённый текст extra-поля multi_choice по pk варианта."""
    if not ans:
        return ""
    return (ans.get("extras") or {}).get(str(choice_pk), "")


@register.filter
def mc_extra_amount(ans, choice_pk):
    """Возвращает сохранённую сумму для name_amount extra-поля multi_choice."""
    if not ans:
        return ""
    return (ans.get("extras_amount") or {}).get(str(choice_pk), "")


@register.filter
def answer_in(ans, choice_pk):
    """Проверяет, есть ли choice_pk в списке ответов multi_choice."""
    if not ans:
        return False
    v = ans.get("v")
    if isinstance(v, list):
        return str(choice_pk) in v
    return False
