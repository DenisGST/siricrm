from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('monitoring/', views.monitoring_dashboard, name='monitoring_dashboard'),
    path('monitoring/api/', views.monitoring_api, name='monitoring_api'),
    path('monitoring/clear-log/', views.monitoring_clear_log, name='monitoring_clear_log'),

    # Панель управления
    path('admin-panel/', views.admin_panel, name='admin_panel'),
    path('admin-panel/departments/', views.admin_departments, name='admin_departments'),
    path('admin-panel/department/add/', views.admin_department_edit, name='admin_department_add'),
    path('admin-panel/department/<uuid:pk>/', views.admin_department_edit, name='admin_department_edit'),
    path('admin-panel/department/<uuid:pk>/delete/', views.admin_department_delete, name='admin_department_delete'),
    path('admin-panel/employees/', views.admin_employees, name='admin_employees'),
    path('admin-panel/employee/add/', views.admin_employee_create, name='admin_employee_create'),
    path('admin-panel/employee/<int:pk>/', views.admin_employee_edit, name='admin_employee_edit'),
    path('admin-panel/dashboards/', views.admin_dashboards, name='admin_dashboards'),
    path('admin-panel/dashboard/add/', views.admin_dashboard_edit, name='admin_dashboard_add'),
    path('admin-panel/dashboard/<uuid:pk>/', views.admin_dashboard_edit, name='admin_dashboard_edit'),
    path('admin-panel/dashboard/<uuid:pk>/delete/', views.admin_dashboard_delete, name='admin_dashboard_delete'),
    path('admin-panel/menu-items/', views.admin_menu_items, name='admin_menu_items'),
    path('admin-panel/menu-item/add/', views.admin_menu_item_edit, name='admin_menu_item_add'),
    path('admin-panel/menu-item/<uuid:pk>/', views.admin_menu_item_edit, name='admin_menu_item_edit'),
    path('admin-panel/menu-item/<uuid:pk>/delete/', views.admin_menu_item_delete, name='admin_menu_item_delete'),
    path('admin-panel/widgets/', views.admin_widgets, name='admin_widgets'),
    path('admin-panel/widget/add/', views.admin_widget_edit, name='admin_widget_add'),
    path('admin-panel/widget/<uuid:pk>/', views.admin_widget_edit, name='admin_widget_edit'),
    path('admin-panel/widget/<uuid:pk>/delete/', views.admin_widget_delete, name='admin_widget_delete'),

    # Справочники (доступ: admin, head_dep)
    path('references/', views.references_panel, name='references_panel'),
    path('references/regions/', views.references_regions, name='references_regions'),
    path('references/region/add/', views.reference_region_edit, name='reference_region_add'),
    path('references/region/<int:pk>/', views.reference_region_edit, name='reference_region_edit'),
    path('references/region/<int:pk>/delete/', views.reference_region_delete, name='reference_region_delete'),
    path('references/kinds/', views.references_kinds, name='references_kinds'),
    path('references/kind/add/', views.reference_kind_edit, name='reference_kind_add'),
    path('references/kind/<uuid:pk>/', views.reference_kind_edit, name='reference_kind_edit'),
    path('references/kind/<uuid:pk>/delete/', views.reference_kind_delete, name='reference_kind_delete'),
]
