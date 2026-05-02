from django.db import migrations


MENU_ITEMS = [
    {
        "url": "/services/",
        "name": "Услуги",
        "icon": "file-text",
        "section": "CRM",
        "order": 35,
        "use_htmx": True,
        "requires_superuser": False,
        "requires_elevated": False,
        "is_active": True,
    },
    {
        "url": "/services-kanban/",
        "name": "Канбан по услугам",
        "icon": "clipboard-list",
        "section": "CRM",
        "order": 36,
        "use_htmx": True,
        "requires_superuser": False,
        "requires_elevated": False,
        "is_active": True,
    },
    {
        "url": "/my-kanban/",
        "name": "Мой Канбан",
        "icon": "star",
        "section": "CRM",
        "order": 37,
        "use_htmx": True,
        "requires_superuser": False,
        "requires_elevated": False,
        "is_active": True,
    },
]


def seed(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    DashboardConfig = apps.get_model("core", "DashboardConfig")
    for payload in MENU_ITEMS:
        url = payload.pop("url")
        mi, _ = MenuItem.objects.update_or_create(url=url, defaults=payload)
        for dc in DashboardConfig.objects.all():
            dc.menu_items.add(mi)


def unseed(apps, schema_editor):
    MenuItem = apps.get_model("core", "MenuItem")
    MenuItem.objects.filter(
        url__in=["/services/", "/services-kanban/", "/my-kanban/"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0009_employee_services_allowed"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
