from django.urls import path
from . import views
from .views import employees_online_count, clients_active_count, messages_new_count, lead_count

urlpatterns = [
    path("", views.dashboard, name="dashboard"),  # корень
    path('dashboard/', views.dashboard, name='dashboard'),
    path('kanban/', views.kanban, name='kanban'),
    path("kanban/<str:status>/", views.kanban_column, name="kanban_column"),
    path('clients/', views.clients_list, name='clients_list'),
    path('employees/', views.employees_list, name='employees_list'),
    path('clients/<uuid:client_id>/chat/', views.chat, name='chat'),
    path("clients/new/", views.client_create, name="client_create"),
    path("clients/<uuid:client_id>/events/", views.client_events_modal, name="client_events_modal"),
    path("clients/<uuid:client_id>/max/send/", views.max_send_message, name="max_send_message"),
    path("clients/<uuid:client_id>/edit/", views.client_edit, name="client_edit"),
    path('logs/', views.logs_list, name='logs_list'),
    path("dashboard/stats/employee-online/", employees_online_count, name="employees_online_count"),
    path("dashboard/stats/client-active/", clients_active_count, name="clients_active_count"),
    path("dashboard/stats/message-new/", messages_new_count, name="messages_new_count"),
    path("dashboard/stats/lead/", lead_count, name="lead_count"),
    path("telegram/clients/", views.telegram_clients_list, name="telegram_clients_list"),
    path("telegram/chat/<uuid:client_id>/", views.telegram_chat_for_client, name="telegram_chat_for_client"),
    path("telegram/chat/<uuid:client_id>/send/", views.telegram_send_message, name="telegram_send_message"),
    path("telegram/chat/<uuid:client_id>/import-history/", views.telegram_import_history, name="telegram_import_history"),
    path("task-status/<str:task_id>/", views.task_status, name="task_status"),
    path("clients/merge-search/", views.client_merge_search, name="client_merge_search"),
    path("clients/<uuid:client_id>/merge/", views.client_merge, name="client_merge"),
    path("message/<uuid:msg_id>/react/", views.message_react, name="message_react"),
    # Адреса клиента
    path("clients/<uuid:client_id>/addresses/", views.client_addresses, name="client_addresses"),
    path("clients/<uuid:client_id>/address/add/", views.address_form, name="address_add"),
    path("clients/<uuid:client_id>/address/<uuid:address_id>/", views.address_form, name="address_edit"),
    path("clients/<uuid:client_id>/address/<uuid:address_id>/delete/", views.address_delete, name="address_delete"),
    path("clients/<uuid:client_id>/close-dialog/", views.cycle_dialog_status, name="close_dialog"),
    path("clients/<uuid:client_id>/move/", views.client_move, name="client_move"),
    path("clients/<uuid:client_id>/assign-employee/", views.client_assign_employee_picker, name="client_assign_employee_picker"),
    path("clients/<uuid:client_id>/assign-employee/set/", views.client_assign_employee, name="client_assign_employee"),
    path("clients/<uuid:client_id>/messenger-status/", views.messenger_status_badge, name="messenger_status_badge"),
    path("api/notifications/count/", views.notifications_count, name="notifications_count"),
    path("api/global-search/", views.global_search, name="global_search"),
    # Юридические лица
    path("legal-entities/", views.legal_entities_list, name="legal_entities_list"),
    path("legal-entities/new/", views.legal_entity_create, name="legal_entity_create"),
    path("legal-entities/<uuid:le_id>/", views.legal_entity_detail, name="legal_entity_detail"),
    path("legal-entities/<uuid:le_id>/edit/", views.legal_entity_edit, name="legal_entity_edit"),

    # Услуги
    path("services/", views.services_list, name="services_list"),
    path("services/new/", views.service_edit, name="service_create"),
    path("services/<uuid:pk>/", views.service_edit, name="service_edit"),
    path("services/<uuid:pk>/delete/", views.service_delete, name="service_delete"),
    path("services/client-search/", views.service_client_search, name="service_client_search"),

    # Канбаны услуг
    path("services/<uuid:pk>/move/", views.service_move, name="service_move"),
    path("services/<uuid:pk>/my-move/", views.service_my_move, name="service_my_move"),
    path("services/<uuid:pk>/employees/picker/", views.service_employee_picker, name="service_employee_picker"),
    path("services/<uuid:pk>/employees/toggle/", views.service_employee_toggle, name="service_employee_toggle"),
    path("services-kanban/", views.services_kanban, name="services_kanban"),
    path("services-kanban/col/<uuid:status_id>/", views.services_kanban_column, name="services_kanban_column"),
    path("my-kanban/", views.my_kanban, name="my_kanban"),
    path("my-kanban/col/<uuid:status_id>/", views.my_kanban_column, name="my_kanban_column"),
]

