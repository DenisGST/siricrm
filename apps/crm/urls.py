from django.urls import path
from . import views
from .views import employees_online_count, clients_active_count, messages_new_count, lead_count

urlpatterns = [
    path("", views.dashboard, name="dashboard"),  # корень
    path('dashboard/', views.dashboard, name='dashboard'),
    path('kanban/', views.kanban, name='kanban'),
    path("kanban/<str:status>/", views.kanban_column, name="kanban_column"),
    path('clients/', views.clients_list, name='clients_list'),
    path('clients/<uuid:client_id>/chat/', views.chat, name='chat'),
    path("clients/new/", views.client_create, name="client_create"),
    path('logs/', views.logs_list, name='logs_list'),
    path("dashboard/stats/employee-online/", employees_online_count, name="employees_online_count"),
    path("dashboard/stats/client-active/", clients_active_count, name="clients_active_count"),
    path("dashboard/stats/message-new/", messages_new_count, name="messages_new_count"),
    path("dashboard/stats/lead/", lead_count, name="lead_count"),
    path("telegram/clients/", views.telegram_clients_list, name="telegram_clients_list"),
    path("telegram/chat/<uuid:client_id>/", views.telegram_chat_for_client, name="telegram_chat_for_client"),
    path("telegram/chat/<uuid:client_id>/send/", views.telegram_send_message, name="telegram_send_message"),

]

