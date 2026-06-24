from apps.core.models import MenuItem, DashboardConfig
from apps.core.permissions import is_references_access, can_handle_scans
from apps.accounting.permissions import can_access_accounting
from apps.procedure.permissions import can_access_procedures


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

    # «Входящие сканы» (/scans/) — отдельный флаг can_handle_scans, которого
    # нет в модели MenuItem; пункт заведён без requires_elevated, поэтому
    # вручную прячем его у тех, кому лоток не положен.
    sections = {}
    show_scans = can_handle_scans(user)
    show_accounting = can_access_accounting(user)
    show_procedures = can_access_procedures(user)
    for item in items.order_by("section", "order"):
        if item.url.startswith("/scans/") and not show_scans:
            continue
        if item.url.startswith("/accounting/") and not show_accounting:
            continue
        if item.url == "/procedure/" and not show_procedures:
            continue
        key = item.section or ""
        sections.setdefault(key, []).append(item)

    return {"sidebar_sections": sections}
