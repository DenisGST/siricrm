from django.urls import path

from . import views

app_name = "efrsb"

urlpatterns = [
    # Вкладка «Публикации» (контейнер) + под-вкладки
    path("service/<uuid:service_id>/tab/publications/", views.tab_publications, name="tab_publications"),
    path("service/<uuid:service_id>/efrsb/", views.subtab_efrsb, name="subtab_efrsb"),
    path("service/<uuid:service_id>/kommersant/", views.subtab_kommersant, name="subtab_kommersant"),
    # Поиск должника / мониторинг
    path("service/<uuid:service_id>/efrsb/resolve/", views.resolve_bankrupt, name="resolve_bankrupt"),
    path("service/<uuid:service_id>/efrsb/confirm/", views.confirm_bankrupt, name="confirm_bankrupt"),
    path("service/<uuid:service_id>/efrsb/refresh/", views.refresh_now, name="refresh_now"),
    # Публикации (наши заготовки)
    path("service/<uuid:service_id>/efrsb/add/", views.publication_add, name="publication_add"),
    path("service/<uuid:service_id>/efrsb/<uuid:pub_id>/generate/", views.publication_generate_form, name="publication_generate_form"),
    path("service/<uuid:service_id>/efrsb/<uuid:pub_id>/generate/save/", views.publication_generate, name="publication_generate"),
    path("service/<uuid:service_id>/efrsb/<uuid:pub_id>/delete/", views.publication_delete, name="publication_delete"),
    # Справочник «Типы сообщений ЕФРСБ»
    path("references/message-types/", views.references_message_types, name="references_message_types"),
    path("references/message-type/add/", views.reference_message_type_edit, name="reference_message_type_add"),
    path("references/message-type/<uuid:pk>/", views.reference_message_type_edit, name="reference_message_type_edit"),
    path("references/message-type/<uuid:pk>/delete/", views.reference_message_type_delete, name="reference_message_type_delete"),
]
