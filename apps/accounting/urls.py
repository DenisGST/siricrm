from django.urls import path

from . import views

app_name = "accounting"

urlpatterns = [
    path("", views.panel, name="panel"),
    path("tab/bank/", views.tab_bank, name="tab_bank"),
    path("tab/notifications/", views.tab_notifications, name="tab_notifications"),
    path("tab/payments/", views.tab_payments, name="tab_payments"),

    # Платёжка (детали) + привязка входящего платежа
    path("payment/detail/", views.payment_detail, name="payment_detail"),
    path("bind/modal/", views.bind_modal, name="bind_modal"),
    path("bind/client-search/", views.bind_client_search, name="bind_client_search"),
    path("bind/charges/", views.bind_charges, name="bind_charges"),
    path("bind/execute/", views.bind_execute, name="bind_execute"),
    path("bind/unidentified/", views.mark_unidentified_view, name="mark_unidentified"),

    # Мониторинг
    path("poll-now/", views.poll_now, name="poll_now"),

    # Эквайринг (публичные эндпоинты для страницы оплаты fo-y.ru)
    path("acquiring/prepay/", views.acquiring_prepay, name="acquiring_prepay"),
    path("acquiring/webhook/", views.acquiring_webhook, name="acquiring_webhook"),
]
