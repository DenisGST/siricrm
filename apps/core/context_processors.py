from apps.core.models import MenuItem, DashboardConfig
from apps.core.permissions import is_references_access


def sidebar_menu(request):
    if not request.user.is_authenticated:
        return {}

    config = None
    if hasattr(request.user, "employee") and request.user.employee.dashboard_config_id:
        config = request.user.employee.dashboard_config

    if not config:
        config = DashboardConfig.objects.filter(is_default=True, is_active=True).first()

    if config:
        items = config.menu_items.filter(is_active=True)
    else:
        items = MenuItem.objects.filter(is_active=True)

    user = request.user
    is_elevated = is_references_access(user)

    if not user.is_superuser:
        items = items.filter(requires_superuser=False)
    if not is_elevated:
        items = items.filter(requires_elevated=False)

    sections = {}
    for item in items.order_by("section", "order"):
        key = item.section or ""
        sections.setdefault(key, []).append(item)

    return {"sidebar_sections": sections}
