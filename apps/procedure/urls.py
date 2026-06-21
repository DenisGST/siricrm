from django.urls import path

from . import views

app_name = "procedure"

urlpatterns = [
    # Лендинг «Юрист БФЛ» (пункт меню) + открытие дела клиента из поиска
    path("", views.panel, name="panel"),
    path("open/", views.open_client_case, name="open_client_case"),
    # Карточка дела (полноэкранный своп в #content-area)
    path("service/<uuid:service_id>/card/", views.procedure_card, name="card"),
    # Вкладки (HTMX-партиалы в #procedure-tab-body)
    path("service/<uuid:service_id>/tab/overview/", views.tab_overview, name="tab_overview"),
    path("service/<uuid:service_id>/stages-bar/", views.stages_bar, name="stages_bar"),
    path("service/<uuid:service_id>/tab/court/", views.tab_court, name="tab_court"),
    path("service/<uuid:service_id>/tab/correspondence/", views.tab_correspondence, name="tab_correspondence"),
    # Запросы (внутри «Корреспонденции»)
    path("service/<uuid:service_id>/requests/recipient-search/", views.recipient_search, name="recipient_search"),
    path("service/<uuid:service_id>/requests/add/", views.request_add, name="request_add"),
    path("service/<uuid:service_id>/requests/package-add/", views.request_package_add, name="request_package_add"),
    path("service/<uuid:service_id>/requests/<uuid:req_id>/sent/", views.request_sent_form, name="request_sent_form"),
    path("service/<uuid:service_id>/requests/<uuid:req_id>/sent/save/", views.request_sent, name="request_sent"),
    path("service/<uuid:service_id>/requests/<uuid:req_id>/response/", views.request_response_form, name="request_response_form"),
    path("service/<uuid:service_id>/requests/<uuid:req_id>/response/save/", views.request_response, name="request_response"),
    path("service/<uuid:service_id>/requests/<uuid:req_id>/delete/", views.request_delete, name="request_delete"),
    path("service/<uuid:service_id>/tab/<str:tab>/", views.tab_placeholder, name="tab_placeholder"),
    # Действия по делу/процедурам
    path("service/<uuid:service_id>/case/", views.update_case_block, name="update_case_block"),
    path("service/<uuid:service_id>/procedure/add/", views.add_procedure, name="add_procedure"),
    path("service/<uuid:service_id>/procedure/<uuid:proc_id>/save/", views.update_procedure, name="update_procedure"),
    path("service/<uuid:service_id>/procedure/<uuid:proc_id>/delete/", views.delete_procedure, name="delete_procedure"),
    path("service/<uuid:service_id>/stage/", views.set_stage, name="set_stage"),
    path("service/<uuid:service_id>/milestone/add/", views.milestone_add, name="milestone_add"),
    path("milestone/<uuid:pk>/status/", views.milestone_set_status, name="milestone_set_status"),
    # Данные должника/супруги (правка карточки Client)
    path("service/<uuid:service_id>/person/<str:who>/", views.update_person, name="update_person"),
    # Супруга (Client.spouse)
    path("service/<uuid:service_id>/spouse/search/", views.spouse_search, name="spouse_search"),
    path("service/<uuid:service_id>/spouse/pick/", views.spouse_pick, name="spouse_pick"),
    path("service/<uuid:service_id>/spouse/link/", views.spouse_link, name="spouse_link"),
    path("service/<uuid:service_id>/spouse/create/", views.spouse_create, name="spouse_create"),
    # Адреса должника/супруги (полный CRUD, who = debtor/spouse)
    path("service/<uuid:service_id>/addr/<str:who>/", views.addresses_block, name="addresses_block"),
    path("service/<uuid:service_id>/addr/<str:who>/new/", views.address_form, name="address_add"),
    path("service/<uuid:service_id>/addr/<str:who>/<uuid:address_id>/", views.address_form, name="address_edit"),
    path("service/<uuid:service_id>/addr/<str:who>/<uuid:address_id>/delete/", views.address_delete, name="address_delete"),
    # Телефоны должника/супруги (who = debtor/spouse)
    path("service/<uuid:service_id>/phones/<str:who>/", views.phones_block, name="phones_block"),
    path("service/<uuid:service_id>/phones/<str:who>/add/", views.phones_add, name="phones_add"),
    path("service/<uuid:service_id>/phones/<str:who>/<uuid:phone_id>/purpose/", views.phones_set_purpose, name="phones_set_purpose"),
    path("service/<uuid:service_id>/phones/<str:who>/<uuid:phone_id>/delete/", views.phones_delete, name="phones_delete"),
    # Справочник «Шаблоны мероприятий» (в разделе «Справочники», вместо админки)
    path("references/milestones/", views.references_milestones, name="references_milestones"),
    path("references/milestone/add/", views.reference_milestone_edit, name="reference_milestone_add"),
    path("references/milestone/<uuid:pk>/", views.reference_milestone_edit, name="reference_milestone_edit"),
    path("references/milestone/<uuid:pk>/delete/", views.reference_milestone_delete, name="reference_milestone_delete"),
    # Справочник «Типы запросов» / «Пакеты запросов»
    path("references/recipient-search/", views.reference_recipient_search, name="reference_recipient_search"),
    path("references/request-types/", views.references_request_types, name="references_request_types"),
    path("references/request-type/add/", views.reference_request_type_edit, name="reference_request_type_add"),
    path("references/request-type/<uuid:pk>/", views.reference_request_type_edit, name="reference_request_type_edit"),
    path("references/request-type/<uuid:pk>/delete/", views.reference_request_type_delete, name="reference_request_type_delete"),
    path("references/request-packages/", views.references_request_packages, name="references_request_packages"),
    path("references/request-package/add/", views.reference_request_package_edit, name="reference_request_package_add"),
    path("references/request-package/<uuid:pk>/", views.reference_request_package_edit, name="reference_request_package_edit"),
    path("references/request-package/<uuid:pk>/delete/", views.reference_request_package_delete, name="reference_request_package_delete"),
]
