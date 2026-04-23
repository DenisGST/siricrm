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
    path("clients/<uuid:client_id>/messenger-status/", views.messenger_status_badge, name="messenger_status_badge"),
    path("api/notifications/count/", views.notifications_count, name="notifications_count"),
    path("api/global-search/", views.global_search, name="global_search"),
    # Юридические лица
    path("legal-entities/", views.legal_entities_list, name="legal_entities_list"),
    path("legal-entities/new/", views.legal_entity_create, name="legal_entity_create"),
    path("legal-entities/<uuid:le_id>/", views.legal_entity_detail, name="legal_entity_detail"),
    path("legal-entities/<uuid:le_id>/edit/", views.legal_entity_edit, name="legal_entity_edit"),
]

