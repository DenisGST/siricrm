from apps.core.models import MenuItem, DashboardConfig


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

    if not request.user.is_superuser:
        items = items.filter(requires_superuser=False)

    sections = {}
    for item in items.order_by("section", "order"):
        key = item.section or ""
        sections.setdefault(key, []).append(item)

    return {"sidebar_sections": sections}
