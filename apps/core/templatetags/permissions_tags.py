"""Шаблонные фильтры для проверок прав. Использование:

    {% load permissions_tags %}
    {% if user|can_view_all_clients %}…{% endif %}
"""
from django import template

from apps.core import permissions

register = template.Library()


@register.filter
def can_view_all_clients(user):
    return permissions.can_view_all_clients(user)


@register.filter
def is_management(user):
    return permissions.is_management(user)
