from django.db import migrations


def seed_data(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    Widget = apps.get_model("core", "Widget")
    DashboardConfig = apps.get_model("core", "DashboardConfig")

    items = [
        MenuItem(name="Главная", icon="🏠", url="/", section="", order=0, use_htmx=False),
        MenuItem(name="Dashboard", icon="📊", url="/dashboard/", section="", order=1, use_htmx=False),
        MenuItem(name="Kanban", icon="📋", url="/kanban/", section="CRM", order=10, use_htmx=True),
        MenuItem(name="Клиенты", icon="👥", url="/clients/", section="CRM", order=11, use_htmx=True),
        MenuItem(name="Сотрудники", icon="👨‍💼", url="/employees/", section="CRM", order=12, use_htmx=True),
        MenuItem(name="Логи", icon="📊", url="/logs/", section="CRM", order=13, use_htmx=True),
        MenuItem(name="Django Admin", icon="⚙️", url="/admin/", section="Администрирование", order=90, use_htmx=False, requires_superuser=True),
        MenuItem(name="Панель управления", icon="🛠️", url="/admin-panel/", section="Администрирование", order=91, use_htmx=True, requires_superuser=True),
        MenuItem(name="Настройки", icon="🔧", url="/settings/", section="Администрирование", order=92, use_htmx=False),
    ]
    MenuItem.objects.bulk_create(items)

    widgets = [
        Widget(name="Активные сотрудники", slug="active-employees", widget_type="stats", order=0),
        Widget(name="Активные клиенты", slug="active-clients", widget_type="stats", order=1),
        Widget(name="Новые сообщения", slug="new-messages", widget_type="stats", order=2),
        Widget(name="Лиды", slug="leads", widget_type="stats", order=3),
        Widget(name="Статус Telegram-бота", slug="telegram-status", widget_type="custom", order=4),
        Widget(name="Мессенджер", slug="messenger", widget_type="custom", order=10),
    ]
    Widget.objects.bulk_create(widgets)

    config = DashboardConfig.objects.create(
        name="Администратор",
        description="Полный доступ ко всем пунктам меню и виджетам",
        is_default=True,
    )
    config.menu_items.set(MenuItem.objects.all())
    config.widgets.set(Widget.objects.all())


def rollback(apps, schema_editor):
    apps.get_model("core", "DashboardConfig").objects.all().delete()
    apps.get_model("core", "Widget").objects.all().delete()
    apps.get_model("core", "MenuItem").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_menu_widget_dashboard_config"),
    ]

    operations = [
        migrations.RunPython(seed_data, rollback),
    ]
