"""Права доступа к руководству пользователя (/wiki/).

Читать руководство может ЛЮБОЙ авторизованный сотрудник — гейт только
@login_required, ролевых ограничений нет (это инструкция по работе с CRM,
она нужна всем).

Править статьи может тот же круг, что и справочники — is_references_access
(суперюзер / админ / руководство). Гейт вьюх-редактора — декоратор
require_wiki_edit.
"""
from functools import wraps

from django.http import HttpResponseForbidden

from apps.core.permissions import is_references_access


def can_edit_wiki(user) -> bool:
    """Может ли пользователь править статьи руководства."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return is_references_access(user)


def require_wiki_edit(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_edit_wiki(request.user):
            return HttpResponseForbidden("Нет прав на редактирование руководства")
        return view_func(request, *args, **kwargs)

    return _wrapped
