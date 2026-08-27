from django.urls import path

from . import views

app_name = "callcenter"

urlpatterns = [
    # Доска оператора
    path("", views.board, name="board"),
    path("column/<uuid:column_id>/", views.column, name="column"),
    path("card/move/", views.card_move, name="card_move"),
    path("card/add/", views.card_add_modal, name="card_add_modal"),
    path("card/add/search/", views.card_add_search, name="card_add_search"),
    path("card/add/<uuid:client_id>/", views.card_add, name="card_add"),
    path("card/<uuid:pk>/remove/", views.card_remove, name="card_remove"),
    path("card/<uuid:pk>/spam/", views.card_spam, name="card_spam"),
    path("card/<uuid:pk>/take/", views.card_take, name="card_take"),
    path("card/<uuid:pk>/release/", views.card_release, name="card_release"),
    path("card/<uuid:pk>/action/", views.card_action_modal, name="card_action_modal"),
    path("card/<uuid:pk>/action/save/", views.card_action_save, name="card_action_save"),
    # Результат звонка
    path("call/<uuid:pk>/result/", views.call_result_modal, name="call_result_modal"),
    path("call/<uuid:pk>/result/save/", views.call_result_save, name="call_result_save"),
    path("call/pending/", views.call_result_pending, name="call_result_pending"),
    # Панель управления → вкладка «Колл-центр»: колонки + чёрный список
    path("admin-panel/", views.admin_panel, name="admin_panel"),
    path("admin-panel/columns/", views.admin_columns, name="admin_columns"),
    path("admin-panel/column/add/", views.admin_column_edit, name="admin_column_add"),
    path("admin-panel/column/<uuid:pk>/", views.admin_column_edit, name="admin_column_edit"),
    path("admin-panel/column/<uuid:pk>/delete/", views.admin_column_delete, name="admin_column_delete"),
    path("admin-panel/column/<uuid:pk>/move/<str:direction>/", views.admin_column_move, name="admin_column_move"),
    path("admin-panel/blacklist/", views.admin_blacklist, name="admin_blacklist"),
    path("admin-panel/blacklist/add/", views.blacklist_add, name="blacklist_add"),
    path("admin-panel/blacklist/<uuid:pk>/delete/", views.blacklist_delete, name="blacklist_delete"),
    path("admin-panel/results/", views.admin_results, name="admin_results"),
    path("admin-panel/result/add/", views.admin_result_edit, name="admin_result_add"),
    path("admin-panel/result/<uuid:pk>/", views.admin_result_edit, name="admin_result_edit"),
    path("admin-panel/result/<uuid:pk>/delete/", views.admin_result_delete, name="admin_result_delete"),
]
