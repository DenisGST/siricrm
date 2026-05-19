from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.models import User
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.core.cache import cache
from apps.crm.models import Message, Client
from apps.core.models import Employee, Department, MenuItem, Widget, DashboardConfig
from apps.core.forms import (
    DepartmentForm, EmployeeAdminForm, EmployeeCreateForm, EmployeeFullEditForm,
    MenuItemForm, WidgetForm, DashboardConfigForm,
)
from django.utils import timezone
from datetime import datetime, timedelta
import psutil
import os
import re

# Реэкспорт предикатов прав, чтобы существующие @user_passes_test(is_admin)
# и импорты из apps.core.views продолжали работать.
from apps.core.permissions import (  # noqa: F401
    is_superuser, is_admin, is_references_access,
)


LOG_FILES = {
    'django':   '/app/logs/crm.log',
    'userbot':  '/app/logs/userbot.log',
    'celery':   '/app/logs/celery.log',
    'maxbot':   '/app/logs/maxbot.log',
}


@user_passes_test(is_superuser)
def icons_gallery(request):
    from pathlib import Path
    from django.conf import settings as djsettings

    line_dir = Path(djsettings.BASE_DIR) / "static" / "icons" / "line"
    brand_dir = Path(djsettings.BASE_DIR) / "static" / "icons" / "brand"

    line_icons = sorted(p.stem for p in line_dir.glob("*.svg"))
    brand_icons = sorted(p.stem for p in brand_dir.glob("*.svg")) if brand_dir.exists() else []

    return render(request, "core/icons_gallery.html", {
        "line_icons": line_icons,
        "brand_icons": brand_icons,
    })


@user_passes_test(is_superuser)
def monitoring_dashboard(request):
    context = {'page_title': 'Мониторинг системы'}
    return render(request, 'monitoring/dashboard.html', context)


@user_passes_test(is_superuser)
def monitoring_api(request):
    """API для получения данных мониторинга"""
    try:
        # Системные метрики — cpu_percent(interval=None) не блокирует поток
        cpu_percent = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        net_io = psutil.net_io_counters()

        # Бизнес-метрики — кэшируем на 30 секунд
        now = timezone.now()
        business = cache.get('monitoring_business')
        if business is None:
            hour_ago = now - timedelta(hours=1)
            day_ago = now - timedelta(days=1)
            business = {
                'active_clients': Client.objects.filter(status='active').count(),
                'total_clients': Client.objects.count(),
                'leads_count': Client.objects.filter(status='lead').count(),
                'unread_messages': Message.objects.filter(is_read=False, direction='incoming').count(),
                'messages_last_hour': Message.objects.filter(created_at__gte=hour_ago).count(),
                'messages_last_day': Message.objects.filter(created_at__gte=day_ago).count(),
                'online_employees': Employee.objects.filter(is_online=True).count(),
                'total_employees': Employee.objects.count(),
            }
            cache.set('monitoring_business', business, 30)

        # Логи — кэшируем на 60 секунд, парсинг файлов дорогой
        errors = {}
        for log_key, log_path in LOG_FILES.items():
            cache_key = f'monitoring_errors_{log_key}'
            result = cache.get(cache_key)
            if result is None:
                cleared_at = cache.get(f'monitoring_cleared_{log_key}')
                result = parse_last_errors(log_path, limit=30, cleared_at=cleared_at)
                cache.set(cache_key, result, 60)
            errors[log_key] = result

        data = {
            'system': {
                'cpu_percent': round(cpu_percent, 1),
                'memory_percent': round(memory.percent, 1),
                'memory_used_gb': round(memory.used / (1024**3), 2),
                'memory_total_gb': round(memory.total / (1024**3), 2),
                'disk_percent': round(disk.percent, 1),
                'disk_used_gb': round(disk.used / (1024**3), 2),
                'disk_total_gb': round(disk.total / (1024**3), 2),
                'network_sent_mb': round(net_io.bytes_sent / (1024**2), 2),
                'network_recv_mb': round(net_io.bytes_recv / (1024**2), 2),
            },
            'business': business,
            'errors': errors,
            'timestamp': now.isoformat(),
        }

        return JsonResponse(data)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@require_POST
@user_passes_test(is_superuser)
def monitoring_clear_log(request):
    """Очищает файл лога (truncate) и сбрасывает кэш."""
    log_key = request.POST.get('log')
    if log_key not in LOG_FILES:
        return JsonResponse({'error': 'Unknown log'}, status=400)

    log_path = LOG_FILES[log_key]
    try:
        if os.path.exists(log_path):
            with open(log_path, 'w', encoding='utf-8') as f:
                f.truncate(0)
    except Exception as e:
        return JsonResponse({'error': f'Failed to clear: {e}'}, status=500)

    cache.delete(f'monitoring_errors_{log_key}')
    cache.delete(f'monitoring_cleared_{log_key}')

    return JsonResponse({'ok': True})


def parse_last_errors(log_file, limit=30, cleared_at=None):
    """Парсинг последних ошибок из лог-файла.

    cleared_at — строка 'YYYY-MM-DD HH:MM:SS'; ошибки старше неё скрываются.
    """
    errors = []

    if not os.path.exists(log_file):
        return errors

    cleared_dt = None
    if cleared_at:
        try:
            cleared_dt = datetime.strptime(cleared_at, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass

    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        error_pattern = re.compile(r'(ERROR|CRITICAL|Exception|Traceback)')
        ts_pattern = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

        current_error = None
        for line in reversed(lines):
            if error_pattern.search(line):
                if current_error is None:
                    current_error = {'lines': [], 'timestamp': None, 'dt': None}

                current_error['lines'].insert(0, line.strip())

                ts_match = ts_pattern.search(line)
                if ts_match and current_error['timestamp'] is None:
                    current_error['timestamp'] = ts_match.group(1)
                    try:
                        current_error['dt'] = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        pass
            else:
                if current_error:
                    dt = current_error.get('dt')
                    # Пропускаем ошибки до момента очистки
                    if cleared_dt and dt and dt <= cleared_dt:
                        current_error = None
                        continue

                    errors.append({
                        'text': '\n'.join(current_error['lines']),
                        'timestamp': current_error['timestamp'] or 'Unknown',
                    })
                    current_error = None

                    if len(errors) >= limit:
                        break

        if current_error:
            dt = current_error.get('dt')
            if not (cleared_dt and dt and dt <= cleared_dt):
                errors.append({
                    'text': '\n'.join(current_error['lines']),
                    'timestamp': current_error['timestamp'] or 'Unknown',
                })

    except Exception as e:
        errors.append({'text': f'Error reading log file: {str(e)}', 'timestamp': 'Unknown'})

    return errors[:limit]


# ─────────────────────────────────────────────
# Панель управления (admin panel)
# ─────────────────────────────────────────────


@user_passes_test(is_admin)
def admin_panel(request):
    tab = request.GET.get("tab", "departments")
    return render(request, "core/admin_panel.html", {"active_tab": tab})


@user_passes_test(is_admin)
def admin_departments(request):
    departments = Department.objects.select_related("manager").all()
    return render(request, "core/partials/admin_departments.html", {"departments": departments})


@user_passes_test(is_admin)
def admin_department_edit(request, pk=None):
    dept = get_object_or_404(Department, pk=pk) if pk else None
    if request.method == "POST":
        form = DepartmentForm(request.POST, instance=dept)
        if form.is_valid():
            form.save()
            return HttpResponse(
                headers={"HX-Trigger": "reloadDepartments"}
            )
    else:
        form = DepartmentForm(instance=dept)
    return render(request, "core/partials/department_form_modal.html", {
        "form": form, "dept": dept,
    })


@user_passes_test(is_admin)
@require_POST
def admin_department_delete(request, pk):
    dept = get_object_or_404(Department, pk=pk)
    dept.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadDepartments"})


@user_passes_test(is_admin)
def admin_employees(request):
    sort = request.GET.get("sort", "user__last_name")
    direction = request.GET.get("dir", "asc")
    allowed_sorts = {
        "name": "user__last_name",
        "department": "department__name",
        "role": "role",
        "dashboard": "dashboard_config__name",
        "messenger": "has_messenger_access",
    }
    order_field = allowed_sorts.get(sort, "user__last_name")
    if direction == "desc":
        order_field = f"-{order_field}"
    employees = (
        Employee.objects
        .select_related("user", "department", "dashboard_config")
        .filter(is_active=True)
        .order_by(order_field)
    )
    return render(request, "core/partials/admin_employees.html", {
        "employees": employees,
        "sort": sort,
        "dir": direction,
    })


@user_passes_test(is_admin)
def admin_employee_edit(request, pk):
    """Полное редактирование сотрудника (ФИО, контакты, роль, доступы)."""
    emp = get_object_or_404(Employee.objects.select_related("user"), pk=pk)
    if request.method == "POST":
        form = EmployeeFullEditForm(request.POST, instance=emp)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadEmployees"})
    else:
        form = EmployeeFullEditForm(instance=emp)
    return render(request, "core/partials/employee_edit_modal.html", {
        "form": form, "emp": emp,
    })


@user_passes_test(is_admin)
def admin_employee_settings(request, pk):
    """Узкая форма: только настройки (роль, дашборд, доступы)."""
    emp = get_object_or_404(Employee.objects.select_related("user"), pk=pk)
    if request.method == "POST":
        form = EmployeeAdminForm(request.POST, instance=emp)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadEmployees"})
    else:
        form = EmployeeAdminForm(instance=emp)
    return render(request, "core/partials/employee_form_modal.html", {
        "form": form, "emp": emp,
    })


@user_passes_test(is_admin)
def admin_employee_create(request):
    if request.method == "POST":
        form = EmployeeCreateForm(request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password"],
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
                email=form.cleaned_data.get("email", ""),
            )
            Employee.objects.create(
                user=user,
                patronymic=form.cleaned_data.get("patronymic", ""),
                phone_mobile=form.cleaned_data.get("phone_mobile", ""),
                phone_internal=form.cleaned_data.get("phone_internal", ""),
                department=form.cleaned_data.get("department"),
                role=form.cleaned_data["role"],
                dashboard_config=form.cleaned_data.get("dashboard_config"),
                has_messenger_access=form.cleaned_data.get("has_messenger_access", True),
            )
            return HttpResponse(headers={"HX-Trigger": "reloadEmployees"})
    else:
        form = EmployeeCreateForm()
    return render(request, "core/partials/employee_create_modal.html", {"form": form})


@user_passes_test(is_admin)
def admin_dashboards(request):
    configs = DashboardConfig.objects.prefetch_related("menu_items", "widgets").all()
    return render(request, "core/partials/admin_dashboards.html", {"configs": configs})


@user_passes_test(is_admin)
def admin_dashboard_edit(request, pk=None):
    config = get_object_or_404(DashboardConfig, pk=pk) if pk else None
    if request.method == "POST":
        form = DashboardConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadDashboards"})
    else:
        form = DashboardConfigForm(instance=config)
    return render(request, "core/partials/dashboard_form_modal.html", {
        "form": form, "config": config,
    })


@user_passes_test(is_admin)
@require_POST
def admin_dashboard_delete(request, pk):
    get_object_or_404(DashboardConfig, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadDashboards"})


@user_passes_test(is_admin)
def admin_menu_items(request):
    items = MenuItem.objects.all()
    return render(request, "core/partials/admin_menu_items.html", {"items": items})


@user_passes_test(is_admin)
def admin_menu_item_edit(request, pk=None):
    item = get_object_or_404(MenuItem, pk=pk) if pk else None
    if request.method == "POST":
        form = MenuItemForm(request.POST, instance=item)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadMenuItems"})
    else:
        form = MenuItemForm(instance=item)
    return render(request, "core/partials/menu_item_form_modal.html", {
        "form": form, "item": item,
    })


@user_passes_test(is_admin)
@require_POST
def admin_menu_item_delete(request, pk):
    get_object_or_404(MenuItem, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadMenuItems"})


@user_passes_test(is_admin)
def admin_widgets(request):
    widgets = Widget.objects.all()
    return render(request, "core/partials/admin_widgets.html", {"widgets": widgets})


@user_passes_test(is_admin)
def admin_widget_edit(request, pk=None):
    widget = get_object_or_404(Widget, pk=pk) if pk else None
    if request.method == "POST":
        form = WidgetForm(request.POST, instance=widget)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadWidgets"})
    else:
        form = WidgetForm(instance=widget)
    return render(request, "core/partials/widget_form_modal.html", {
        "form": form, "widget": widget,
    })


@user_passes_test(is_admin)
@require_POST
def admin_widget_delete(request, pk):
    get_object_or_404(Widget, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadWidgets"})


# ─────────────────────────────────────────────
# Справочники (references): доступ руководителям и администраторам
# ─────────────────────────────────────────────


@user_passes_test(is_references_access)
def references_panel(request):
    from apps.core.forms import RegionForm, LegalEntityKindForm  # noqa: F401
    tab = request.GET.get("tab", "regions")
    return render(request, "core/references_panel.html", {"active_tab": tab})


@user_passes_test(is_references_access)
def references_regions(request):
    from apps.crm.models import Region
    regions = Region.objects.order_by("number")
    return render(request, "core/partials/references_regions.html", {"regions": regions})


@user_passes_test(is_references_access)
def reference_region_edit(request, pk=None):
    from django.conf import settings
    from apps.crm.models import Region
    from apps.core.forms import RegionForm
    region = get_object_or_404(Region, pk=pk) if pk else None
    if request.method == "POST":
        form = RegionForm(request.POST, instance=region)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadRegions"})
    else:
        form = RegionForm(instance=region)
    return render(request, "core/partials/region_form_modal.html", {
        "form": form, "region": region,
        "dadata_api_key": settings.DADATA_API_KEY,
    })


@user_passes_test(is_references_access)
@require_POST
def reference_region_delete(request, pk):
    from apps.crm.models import Region
    get_object_or_404(Region, pk=pk).delete()
    return HttpResponse(headers={"HX-Trigger": "reloadRegions"})


@user_passes_test(is_references_access)
def references_kinds(request):
    from apps.crm.models import LegalEntityKind
    kinds = LegalEntityKind.objects.order_by("name")
    return render(request, "core/partials/references_kinds.html", {"kinds": kinds})


@user_passes_test(is_references_access)
def reference_kind_edit(request, pk=None):
    from apps.crm.models import LegalEntityKind
    from apps.core.forms import LegalEntityKindForm
    kind = get_object_or_404(LegalEntityKind, pk=pk) if pk else None
    if request.method == "POST":
        form = LegalEntityKindForm(request.POST, instance=kind)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadKinds"})
    else:
        form = LegalEntityKindForm(instance=kind)
    return render(request, "core/partials/kind_form_modal.html", {
        "form": form, "kind": kind,
    })


@user_passes_test(is_references_access)
@require_POST
def reference_kind_delete(request, pk):
    from apps.crm.models import LegalEntityKind
    kind = get_object_or_404(LegalEntityKind, pk=pk)
    # Проверяем, не используется ли тип юрлицами — иначе PROTECT выдаст ошибку.
    if kind.legal_entities.exists():
        return HttpResponse(
            f"Нельзя удалить: тип используется в {kind.legal_entities.count()} юрлицах.",
            status=409,
        )
    kind.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadKinds"})


# ─── Справочник: Услуги (ServiceName) ───
@user_passes_test(is_references_access)
def references_service_names(request):
    from apps.crm.models import ServiceName
    items = ServiceName.objects.prefetch_related("departments").order_by("short_name")
    return render(request, "core/partials/references_service_names.html", {"items": items})


@user_passes_test(is_references_access)
def reference_service_name_edit(request, pk=None):
    from apps.crm.models import ServiceName
    from apps.core.forms import ServiceNameForm
    obj = get_object_or_404(ServiceName, pk=pk) if pk else None
    if request.method == "POST":
        form = ServiceNameForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadServiceNames"})
    else:
        form = ServiceNameForm(instance=obj)
    return render(request, "core/partials/service_name_form_modal.html", {
        "form": form, "obj": obj,
    })


@user_passes_test(is_references_access)
@require_POST
def reference_service_name_delete(request, pk):
    from apps.crm.models import ServiceName
    obj = get_object_or_404(ServiceName, pk=pk)
    if obj.services.exists():
        return HttpResponse(
            f"Нельзя удалить: услуга используется в {obj.services.count()} договорах.",
            status=409,
        )
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadServiceNames"})


# ─── Справочник: Порядок оплаты ───
@user_passes_test(is_references_access)
def references_payment_procedures(request):
    from apps.crm.models import PaymentProcedure
    items = PaymentProcedure.objects.order_by("short_name")
    return render(request, "core/partials/references_payment_procedures.html", {"items": items})


@user_passes_test(is_references_access)
def reference_payment_procedure_edit(request, pk=None):
    from apps.crm.models import PaymentProcedure
    from apps.core.forms import PaymentProcedureForm
    obj = get_object_or_404(PaymentProcedure, pk=pk) if pk else None
    if request.method == "POST":
        form = PaymentProcedureForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadPaymentProcedures"})
    else:
        form = PaymentProcedureForm(instance=obj)
    return render(request, "core/partials/payment_procedure_form_modal.html", {
        "form": form, "obj": obj,
    })


@user_passes_test(is_references_access)
@require_POST
def reference_payment_procedure_delete(request, pk):
    from apps.crm.models import PaymentProcedure
    obj = get_object_or_404(PaymentProcedure, pk=pk)
    if obj.services.exists():
        return HttpResponse(
            f"Нельзя удалить: порядок оплаты используется в {obj.services.count()} договорах.",
            status=409,
        )
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadPaymentProcedures"})


# ─── Справочник: Общие статусы услуг ───
@user_passes_test(is_references_access)
def references_common_statuses(request):
    from apps.crm.models import ServiceCommonStatus
    items = ServiceCommonStatus.objects.select_related("service_name").order_by("service_name", "order")
    return render(request, "core/partials/references_common_statuses.html", {"items": items})


@user_passes_test(is_references_access)
def reference_common_status_edit(request, pk=None):
    from apps.crm.models import ServiceCommonStatus
    from apps.core.forms import ServiceCommonStatusForm
    obj = get_object_or_404(ServiceCommonStatus, pk=pk) if pk else None
    if request.method == "POST":
        form = ServiceCommonStatusForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadCommonStatuses"})
    else:
        form = ServiceCommonStatusForm(instance=obj)
    return render(request, "core/partials/common_status_form_modal.html", {
        "form": form, "obj": obj,
    })


@user_passes_test(is_references_access)
@require_POST
def reference_common_status_delete(request, pk):
    from apps.crm.models import ServiceCommonStatus
    obj = get_object_or_404(ServiceCommonStatus, pk=pk)
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadCommonStatuses"})


# ─── Справочник: Статусы услуг сотрудников ───
@user_passes_test(is_references_access)
def references_employee_statuses(request):
    from apps.crm.models import ServiceEmployeeStatus, ServiceCommonStatus, ServiceName
    emp_id = request.GET.get("employee") or ""
    service_id = request.GET.get("service_name") or ""
    common_status_id = request.GET.get("common_status") or ""
    items = ServiceEmployeeStatus.objects.select_related(
        "employee__user", "common_status__service_name"
    ).order_by("employee", "common_status__service_name", "common_status__order", "order")
    if emp_id:
        items = items.filter(employee_id=emp_id)
    if service_id:
        items = items.filter(common_status__service_name_id=service_id)
    if common_status_id:
        items = items.filter(common_status_id=common_status_id)
    employees = Employee.objects.select_related("user").order_by("user__last_name")
    service_names = ServiceName.objects.filter(is_active=True).order_by("short_name")
    common_statuses = ServiceCommonStatus.objects.filter(is_active=True).select_related("service_name")
    if service_id:
        common_statuses = common_statuses.filter(service_name_id=service_id)
    return render(request, "core/partials/references_employee_statuses.html", {
        "items": items,
        "employees_all": employees,
        "service_names": service_names,
        "common_statuses": common_statuses.order_by("service_name__short_name", "order"),
        "filter_employee": emp_id,
        "filter_service_name": service_id,
        "filter_common_status": common_status_id,
    })


@user_passes_test(is_references_access)
def reference_employee_status_edit(request, pk=None):
    from apps.crm.models import ServiceEmployeeStatus
    from apps.core.forms import ServiceEmployeeStatusForm
    obj = get_object_or_404(ServiceEmployeeStatus, pk=pk) if pk else None
    if request.method == "POST":
        form = ServiceEmployeeStatusForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadEmployeeStatuses"})
    else:
        form = ServiceEmployeeStatusForm(instance=obj)
    return render(request, "core/partials/employee_status_form_modal.html", {
        "form": form, "obj": obj,
    })


@user_passes_test(is_references_access)
@require_POST
def reference_employee_status_delete(request, pk):
    from apps.crm.models import ServiceEmployeeStatus
    obj = get_object_or_404(ServiceEmployeeStatus, pk=pk)
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadEmployeeStatuses"})


# ─── Справочник: Теги сотрудников ───
@user_passes_test(is_references_access)
def references_tags(request):
    from apps.crm.models import ServiceTag
    emp_id = request.GET.get("employee") or ""
    items = ServiceTag.objects.select_related("employee__user").order_by("employee", "name")
    if emp_id:
        items = items.filter(employee_id=emp_id)
    employees = Employee.objects.select_related("user").order_by("user__last_name")
    return render(request, "core/partials/references_tags.html", {
        "items": items,
        "employees_all": employees,
        "filter_employee": emp_id,
    })


@user_passes_test(is_references_access)
def reference_tag_edit(request, pk=None):
    from apps.crm.models import ServiceTag
    from apps.core.forms import ServiceTagForm
    obj = get_object_or_404(ServiceTag, pk=pk) if pk else None
    if request.method == "POST":
        form = ServiceTagForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return HttpResponse(headers={"HX-Trigger": "reloadTags"})
    else:
        form = ServiceTagForm(instance=obj)
    return render(request, "core/partials/tag_form_modal.html", {
        "form": form, "obj": obj,
    })


@user_passes_test(is_references_access)
@require_POST
def reference_tag_delete(request, pk):
    from apps.crm.models import ServiceTag
    obj = get_object_or_404(ServiceTag, pk=pk)
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadTags"})


# ─── Справочник: Шаблоны сообщений (для мессенджеров) ───
@user_passes_test(is_references_access)
def references_message_templates(request):
    from apps.crm.models import MessageTemplate
    items = MessageTemplate.objects.all()
    return render(request, "core/partials/references_message_templates.html", {"items": items})


@user_passes_test(is_references_access)
def reference_message_template_edit(request, pk=None):
    from apps.crm.models import MessageTemplate
    from apps.core.forms import MessageTemplateForm

    obj = get_object_or_404(MessageTemplate, pk=pk) if pk else None
    employee = getattr(request.user, "employee", None)

    if request.method == "POST":
        form = MessageTemplateForm(request.POST, instance=obj)
        if form.is_valid():
            instance = form.save(commit=False)
            if not instance.pk:
                instance.created_by = employee
            instance.updated_by = employee
            instance.save()
            return HttpResponse(headers={"HX-Trigger": "reloadMessageTemplates"})
    else:
        initial = {}
        if obj:
            initial["channels"] = obj.channels or []
        form = MessageTemplateForm(instance=obj, initial=initial)

    return render(request, "core/partials/message_template_form_modal.html", {
        "form": form, "obj": obj,
    })


@user_passes_test(is_references_access)
@require_POST
def reference_message_template_delete(request, pk):
    from apps.crm.models import MessageTemplate
    obj = get_object_or_404(MessageTemplate, pk=pk)
    obj.delete()
    return HttpResponse(headers={"HX-Trigger": "reloadMessageTemplates"})
